#!/usr/bin/env python3
"""Complete missing French GNU gettext translations with the OpenAI Responses API.

The editor is deliberately lossless: it only replaces empty msgstr fields and
leaves every other byte from the input PO file untouched.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import html
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gpt-5.6-luna"
FIELD_RE = re.compile(r"^(msgctxt|msgid_plural|msgid|msgstr(?:\[(\d+)\])?)\s+(\".*\")\s*$")
CONTINUATION_RE = re.compile(r'^\s*(".*")\s*$')
PRINTF_RE = re.compile(
	r"%(?:\([^)]+\))?(?:\d+\$)?[#0 +'\-]*(?:\d+|\*)?(?:\.\d+|\.\*)?(?:hh|h|ll|l|L|j|z|t)?[diuoxXfFeEgGaAcspn%](?![A-Za-z])"
)
BRACE_RE = re.compile(r"\{\{.*?\}\}|(?<!\{)\{[^{}]+\}(?!\})|\$\{[^{}]+\}")
HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\([^)]+\)")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'-]*")


@dataclass
class POField:
	name: str
	index: int | None
	value: str
	start: int
	end: int


@dataclass
class POEntry:
	ordinal: int
	start: int
	end: int
	raw_lines: list[str]
	fields: dict[str, POField]
	comments: list[str]
	obsolete: bool

	@property
	def msgid(self) -> str:
		return self.fields.get("msgid", POField("msgid", None, "", 0, 0)).value

	@property
	def msgctxt(self) -> str:
		return self.fields.get("msgctxt", POField("msgctxt", None, "", 0, 0)).value

	@property
	def msgid_plural(self) -> str:
		return self.fields.get("msgid_plural", POField("msgid_plural", None, "", 0, 0)).value

	@property
	def references(self) -> list[str]:
		return [token for line in self.comments if line.startswith("#:") for token in line[2:].strip().split()]

	@property
	def translation_fields(self) -> list[POField]:
		return sorted(
			(value for key, value in self.fields.items() if key == "msgstr" or key.startswith("msgstr[")),
			key=lambda item: -1 if item.index is None else item.index,
		)

	@property
	def key(self) -> str:
		return hashlib.sha256(f"{self.msgctxt}\0{self.msgid}\0{self.msgid_plural}".encode()).hexdigest()[:20]


@dataclass
class Decision:
	entry: POEntry
	old: dict[str, str]
	new: dict[str, str]
	status: str
	comment: str
	glossary_terms: list[str] = dataclass_field(default_factory=list)
	confidence: str = "élevé"
	errors: list[str] = dataclass_field(default_factory=list)


def decode_po_string(literal: str) -> str:
	try:
		value = ast.literal_eval(literal)
	except (SyntaxError, ValueError) as exc:
		raise ValueError(f"chaîne PO invalide: {literal!r}") from exc
	if not isinstance(value, str):
		raise ValueError(f"valeur PO non textuelle: {literal!r}")
	return value


def encode_po_string(value: str) -> str:
	return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n") + '"'


def parse_po(text: str) -> tuple[list[str], list[POEntry]]:
	lines = text.splitlines(keepends=True)
	entries: list[POEntry] = []
	block_start = 0
	ordinal = 0
	for position in range(len(lines) + 1):
		if position < len(lines) and lines[position].strip():
			continue
		if position > block_start:
			block = lines[block_start:position]
			entry = parse_block(block, block_start, position, ordinal)
			if entry:
				entries.append(entry)
				ordinal += 1
		block_start = position + 1
	return lines, entries


def parse_block(block: list[str], absolute_start: int, absolute_end: int, ordinal: int) -> POEntry | None:
	fields: dict[str, POField] = {}
	comments: list[str] = []
	obsolete = any(line.startswith("#~") for line in block)
	i = 0
	while i < len(block):
		line = block[i].rstrip("\r\n")
		parse_line = line[3:] if line.startswith("#~ ") else line
		if line.startswith("#") and not line.startswith("#~ "):
			comments.append(line)
			i += 1
			continue
		match = FIELD_RE.match(parse_line)
		if not match:
			i += 1
			continue
		name, raw_index, literal = match.groups()
		value = decode_po_string(literal)
		j = i + 1
		while j < len(block):
			continuation_line = block[j].rstrip("\r\n")
			if continuation_line.startswith("#~ "):
				continuation_line = continuation_line[3:]
			continuation = CONTINUATION_RE.match(continuation_line)
			if not continuation:
				break
			value += decode_po_string(continuation.group(1))
			j += 1
		index = int(raw_index) if raw_index is not None else None
		fields[name] = POField(name, index, value, absolute_start + i, absolute_start + j)
		i = j
	if "msgid" not in fields:
		return None
	return POEntry(ordinal, absolute_start, absolute_end, block, fields, comments, obsolete)


class Glossary:
	def __init__(self, entries: list[POEntry], external_path: Path | None = None):
		self.by_source: dict[str, Counter[str]] = defaultdict(Counter)
		self.by_context: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
		self.external: dict[str, str] = {}
		for entry in entries:
			if entry.obsolete or not entry.msgid:
				continue
			field = entry.fields.get("msgstr") or entry.fields.get("msgstr[0]")
			if field and field.value:
				self.by_source[entry.msgid][field.value] += 1
				self.by_context[(entry.msgctxt, entry.msgid)][field.value] += 1
		if external_path:
			data = json.loads(external_path.read_text(encoding="utf-8"))
			if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
				raise ValueError("le glossaire externe doit être un objet JSON {anglais: français}")
			self.external = data

	@property
	def conflicts(self) -> dict[str, dict[str, int]]:
		result: dict[str, dict[str, int]] = {}
		for source in self.by_source.keys() | self.external.keys():
			values = dict(self.by_source[source])
			if source in self.external and self.external[source] not in values:
				values[f"[externe] {self.external[source]}"] = 0
			if len(values) > 1:
				result[source] = values
		return result

	def relevant(self, entry: POEntry, limit: int = 16) -> list[dict[str, Any]]:
		target_words = {word.lower() for word in WORD_RE.findall(entry.msgid)}
		scored: list[tuple[float, str, str, int]] = []
		for source in self.by_source.keys() | self.external.keys():
			if not source or len(source) > 160:
				continue
			source_words = {word.lower() for word in WORD_RE.findall(source)}
			overlap = len(target_words & source_words)
			contained = source.lower() in entry.msgid.lower() or entry.msgid.lower() in source.lower()
			if not overlap and not contained:
				continue
			context_targets = self.by_context.get((entry.msgctxt, source))
			if context_targets:
				translation, frequency = context_targets.most_common(1)[0]
			elif self.by_source[source]:
				translation, frequency = self.by_source[source].most_common(1)[0]
			else:
				translation, frequency = self.external[source], 0
			context_bonus = 4 if context_targets else 0
			score = overlap * 3 + context_bonus + (5 if contained else 0) + min(frequency, 5) / 10
			scored.append((score, source, translation, frequency))
		return [
			{"source": source, "translation": translation, "frequency": frequency}
			for _, source, translation, frequency in sorted(scored, reverse=True)[:limit]
		]


def field_label(field: POField) -> str:
	return field.name


def old_values(entry: POEntry) -> dict[str, str]:
	return {field_label(item): item.value for item in entry.translation_fields}


def ensure_translation_fields(entry: POEntry) -> None:
	"""Represent an absent msgstr as empty synthetic fields ready for insertion."""
	if entry.translation_fields:
		return
	names = ["msgstr[0]", "msgstr[1]"] if entry.msgid_plural else ["msgstr"]
	for index, name in enumerate(names):
		entry.fields[name] = POField(name, index if entry.msgid_plural else None, "", entry.end, entry.end)


def technical_tokens(value: str) -> dict[str, list[str]]:
	return {
		"printf": PRINTF_RE.findall(value),
		"braces": BRACE_RE.findall(value),
		"html": HTML_TAG_RE.findall(value),
		"markdown": MARKDOWN_LINK_RE.findall(value),
	}


def validate_translation(source: str, translation: str) -> list[str]:
	errors: list[str] = []
	control_characters = [f"U+{ord(character):04X}" for character in translation if ord(character) < 32 and character not in "\n\r\t"]
	if control_characters:
		errors.append(f"caractères de contrôle interdits: {control_characters!r}")
	for category, source_tokens in technical_tokens(source).items():
		target_tokens = technical_tokens(translation)[category]
		if source_tokens != target_tokens:
			errors.append(f"{category}: attendu {source_tokens!r}, obtenu {target_tokens!r}")
	if len(source) - len(source.lstrip()) != len(translation) - len(translation.lstrip()):
		errors.append("espaces initiaux modifiés")
	if len(source) - len(source.rstrip()) != len(translation) - len(translation.rstrip()):
		errors.append("espaces finaux modifiés")
	if source.count("\n") != translation.count("\n"):
		errors.append("nombre de retours à la ligne modifié")
	return errors


def source_snippets(entry: POEntry, source_root: Path | None) -> list[str]:
	if not source_root:
		return []
	result: list[str] = []
	root = source_root.resolve()
	for reference in entry.references[:3]:
		path_text, separator, line_text = reference.rpartition(":")
		if not separator or not line_text.isdigit():
			path_text, line_text = reference, "1"
		try:
			path = (root / path_text).resolve()
			path.relative_to(root)
			lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
			line_number = max(1, int(line_text))
			start, end = max(0, line_number - 2), min(len(lines), line_number + 1)
			result.append(f"{reference}: " + "\n".join(lines[start:end])[:1000])
		except (OSError, ValueError):
			continue
	return result


def candidate_payload(entry: POEntry, entries: list[POEntry], glossary: Glossary, source_root: Path | None) -> dict[str, Any]:
	missing = [field.name for field in entry.translation_fields if not field.value]
	neighbors = [
		{"msgid": item.msgid, "translation": old_values(item)}
		for item in entries[max(0, entry.ordinal - 2) : entry.ordinal + 3]
		if item is not entry
	]
	return {
		"id": entry.key,
		"msgid": entry.msgid,
		"msgid_plural": entry.msgid_plural or None,
		"msgctxt": entry.msgctxt or None,
		"comments_and_references": entry.comments,
		"source_snippets": source_snippets(entry, source_root),
		"existing_translations": old_values(entry),
		"missing_fields": missing,
		"nearby_entries": neighbors,
		"glossary": glossary.relevant(entry),
	}


SYSTEM_PROMPT = """Tu es traducteur principal d'une application métier Frappe. Traduis de l'anglais vers un français naturel et cohérent.
Priorité absolue au msgctxt, au contexte source et au glossaire fourni. Respecte le wording existant, la casse utile et les conventions typographiques françaises. Les boutons emploient l'infinitif.
Ne traduis que les champs listés dans missing_fields. Ne change jamais existing_translations.
Conserve exactement, dans le même ordre, tous les placeholders printf/Python, accolades/Jinja/JavaScript, balises HTML, liens Markdown, retours à la ligne et espaces de bord.
Pour un pluriel français, msgstr[0] est le singulier et msgstr[1] le pluriel. Si le sens reste réellement ambigu malgré tout le contexte, renvoie status=manual et des traductions vides. Sinon status=translated.
L'explication doit être spécifique: citer le contexte, le choix terminologique ou la contrainte technique réellement utilisée. glossary_terms contient les correspondances source → cible effectivement suivies. Réponds en JSON conforme au schéma."""


def response_schema() -> dict[str, Any]:
	translation = {
		"type": "object",
		"additionalProperties": False,
		"properties": {"field": {"type": "string"}, "value": {"type": "string"}},
		"required": ["field", "value"],
	}
	item = {
		"type": "object",
		"additionalProperties": False,
		"properties": {
			"id": {"type": "string"},
			"translations": {"type": "array", "items": translation},
			"status": {"type": "string", "enum": ["translated", "manual"]},
			"explanation": {"type": "string"},
			"glossary_terms": {"type": "array", "items": {"type": "string"}},
			"confidence": {"type": "string", "enum": ["high", "medium", "low"]},
		},
		"required": ["id", "translations", "status", "explanation", "glossary_terms", "confidence"],
	}
	return {
		"type": "object",
		"additionalProperties": False,
		"properties": {"results": {"type": "array", "items": item}},
		"required": ["results"],
	}


class OpenAIClient:
	def __init__(self, api_key: str, model: str, timeout: float, retries: int):
		self.api_key = api_key
		self.model = model
		self.timeout = timeout
		self.retries = retries

	def translate(self, payloads: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
		body = {
			"model": self.model,
			"reasoning": {"effort": "medium"},
			"input": [
				{"role": "system", "content": SYSTEM_PROMPT},
				{"role": "user", "content": json.dumps({"entries": payloads}, ensure_ascii=False)},
			],
			"text": {
				"format": {
					"type": "json_schema",
					"name": "po_translations",
					"strict": True,
					"schema": response_schema(),
				}
			},
		}
		request = urllib.request.Request(
			"https://api.openai.com/v1/responses",
			data=json.dumps(body).encode(),
			headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
			method="POST",
		)
		last_error: Exception | None = None
		for attempt in range(self.retries + 1):
			try:
				with urllib.request.urlopen(request, timeout=self.timeout) as response:
					data = json.load(response)
				text = data.get("output_text") or extract_output_text(data)
				parsed = json.loads(text)
				return {item["id"]: item for item in parsed["results"]}
			except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, ValueError, json.JSONDecodeError) as exc:
				last_error = exc
				if attempt >= self.retries:
					break
				time.sleep(min(2**attempt, 20))
		raise RuntimeError(f"échec de l'API OpenAI après {self.retries + 1} tentative(s): {last_error}")


def extract_output_text(response: dict[str, Any]) -> str:
	parts: list[str] = []
	for output in response.get("output", []):
		for content in output.get("content", []):
			if content.get("type") == "refusal":
				raise RuntimeError(f"réponse refusée par le modèle: {content.get('refusal', '')}")
			if content.get("type") == "output_text":
				parts.append(content.get("text", ""))
	if not parts:
		raise RuntimeError("la réponse OpenAI ne contient aucun output_text")
	return "".join(parts)


def load_checkpoint(path: Path) -> dict[str, dict[str, Any]]:
	if not path.exists():
		return {}
	data = json.loads(path.read_text(encoding="utf-8"))
	return data if isinstance(data, dict) else {}


def save_json_atomic(path: Path, data: Any) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
		json.dump(data, handle, ensure_ascii=False, indent=2)
		temp_path = Path(handle.name)
	os.replace(temp_path, path)


def enrich_checkpoint_record(entry: POEntry, result: dict[str, Any]) -> dict[str, Any]:
	"""Store source and previous values next to the API proposal for review."""
	result = normalize_checkpoint_result(result)
	return {
		"msgid": entry.msgid,
		"msgid_plural": entry.msgid_plural or None,
		"msgctxt": entry.msgctxt or None,
		"old_translations": old_values(entry),
		"comments": entry.comments,
		"references": entry.references,
		**result,
	}


def normalize_checkpoint_result(result: dict[str, Any]) -> dict[str, Any]:
	"""Repair known JSON escape corruption and copy a result safely."""
	normalized = dict(result)
	translations = result.get("translations", [])
	if isinstance(translations, dict):
		normalized["translations"] = {
			name: value.replace("\x00a0", "\u00a0") if isinstance(value, str) else value
			for name, value in translations.items()
		}
	elif isinstance(translations, list):
		normalized["translations"] = [
			{
				**item,
				"value": item.get("value", "").replace("\x00a0", "\u00a0"),
			}
			if isinstance(item, dict) and isinstance(item.get("value"), str)
			else item
			for item in translations
		]
	return normalized


def decide(entry: POEntry, result: dict[str, Any]) -> Decision:
	old = old_values(entry)
	result = normalize_checkpoint_result(result)
	if result.get("status") == "manual":
		return Decision(entry, old, {}, "validation manuelle requise", result.get("explanation", "Sens ambigu."), confidence="faible")
	raw_translations = result.get("translations", [])
	if isinstance(raw_translations, dict):  # accepts checkpoints made by early script versions
		translations = raw_translations
	else:
		translations = {
			item.get("field"): item.get("value")
			for item in raw_translations
			if isinstance(item, dict) and isinstance(item.get("field"), str)
		}
	missing = {field.name: field for field in entry.translation_fields if not field.value}
	new: dict[str, str] = {}
	errors: list[str] = []
	for name, field in missing.items():
		value = translations.get(name)
		if not isinstance(value, str) or not value:
			errors.append(f"{name}: traduction absente")
			continue
		source = entry.msgid_plural if field.index is not None and field.index > 0 and entry.msgid_plural else entry.msgid
		field_errors = validate_translation(source, value)
		if field_errors:
			errors.extend(f"{name}: {error}" for error in field_errors)
		else:
			new[name] = value
	if errors:
		return Decision(entry, old, {}, "erreur", "Traduction refusée par les validations techniques.", errors=errors, confidence="faible")
	confidence = {"high": "élevé", "medium": "moyen", "low": "faible"}.get(result.get("confidence"), "moyen")
	if confidence == "faible":
		return Decision(entry, old, {}, "validation manuelle requise", result.get("explanation", "Confiance insuffisante."), result.get("glossary_terms", []), confidence)
	return Decision(entry, old, new, "traduction ajoutée", result.get("explanation", "Choix fondé sur le contexte fourni."), result.get("glossary_terms", []), confidence)


def baseline_decisions(entries: list[POEntry]) -> tuple[list[Decision], list[POEntry]]:
	decisions: list[Decision] = []
	candidates: list[POEntry] = []
	for entry in entries:
		old = old_values(entry)
		if entry.obsolete:
			decisions.append(Decision(entry, old, {}, "entrée ignorée", "Entrée gettext obsolète (#~), laissée intacte."))
		elif not entry.msgid:
			decisions.append(Decision(entry, old, {}, "entrée ignorée", "En-tête gettext, laissé intact."))
		else:
			ensure_translation_fields(entry)
			old = old_values(entry)
		if entry.obsolete or not entry.msgid:
			continue
		if all(field.value for field in entry.translation_fields):
			decisions.append(Decision(entry, old, {}, "traduction existante conservée", "Traduction existante conservée strictement à l'identique conformément à la règle de préservation."))
		else:
			candidates.append(entry)
	return decisions, candidates


def apply_decisions(original_lines: list[str], decisions: list[Decision]) -> str:
	replacements: list[tuple[int, int, str]] = []
	for decision in decisions:
		synthetic: list[tuple[str, str]] = []
		for name, value in decision.new.items():
			field = decision.entry.fields[name]
			if field.start == field.end == decision.entry.end:
				synthetic.append((name, value))
				continue
			newline = "\n"
			if field.start < len(original_lines) and original_lines[field.start].endswith("\r\n"):
				newline = "\r\n"
			replacements.append((field.start, field.end, f"{name} {encode_po_string(value)}{newline}"))
		if synthetic:
			newline = "\r\n" if original_lines and original_lines[0].endswith("\r\n") else "\n"
			text = "".join(f"{name} {encode_po_string(value)}{newline}" for name, value in synthetic)
			replacements.append((decision.entry.end, decision.entry.end, text))
	result = original_lines[:]
	for start, end, replacement in sorted(replacements, reverse=True):
		result[start:end] = [replacement]
	return "".join(result)


def assert_existing_preserved(before: list[POEntry], after: list[POEntry]) -> None:
	after_by_key = {(entry.msgctxt, entry.msgid, entry.msgid_plural): entry for entry in after}
	for entry in before:
		updated = after_by_key.get((entry.msgctxt, entry.msgid, entry.msgid_plural))
		if not updated:
			raise RuntimeError(f"entrée perdue après écriture: {entry.msgid!r}")
		for field in entry.translation_fields:
			if field.value and (field.name not in updated.fields or updated.fields[field.name].value != field.value):
				raise RuntimeError(f"traduction existante modifiée: {entry.msgid!r} / {field.name}")


def counts(decisions: list[Decision], conflicts: int) -> dict[str, int]:
	counter = Counter(item.status for item in decisions)
	return {
		"total": len(decisions),
		"conserved": counter["traduction existante conservée"],
		"added": counter["traduction ajoutée"],
		"ignored": counter["entrée ignorée"],
		"errors": counter["erreur"],
		"manual": counter["validation manuelle requise"],
		"conflicts": conflicts,
	}


def render_report(path: Path, source: Path, decisions: list[Decision], glossary: Glossary, dry_run: bool) -> None:
	summary = counts(decisions, len(glossary.conflicts))
	labels = {
		"total": "Entrées analysées", "conserved": "Conservées", "added": "Ajoutées",
		"ignored": "Ignorées", "errors": "Erreurs", "manual": "À valider", "conflicts": "Conflits",
	}
	cards = "".join(f'<div class="card"><b>{value}</b><span>{labels[key]}</span></div>' for key, value in summary.items())
	rows: list[str] = []
	for decision in sorted(decisions, key=lambda item: item.entry.ordinal):
		old = "\n".join(f"{key}: {value}" for key, value in decision.old.items() if value) or "Non traduite"
		combined = decision.old.copy()
		combined.update(decision.new)
		new = "\n".join(f"{key}: {value}" for key, value in combined.items() if value) or "—"
		comment = decision.comment + ((" Erreurs : " + "; ".join(decision.errors)) if decision.errors else "")
		status_class = {
			"traduction ajoutée": "added", "validation manuelle requise": "manual", "erreur": "error",
			"entrée ignorée": "ignored", "traduction existante conservée": "conserved",
		}.get(decision.status, "unknown")
		rows.append(
			f'<tr class="{status_class}"><td>{html.escape(decision.entry.msgid)}</td>'
			f'<td>{html.escape(decision.entry.msgctxt or "—")}</td><td>{html.escape(old)}</td>'
			f'<td>{html.escape(new)}</td><td><span class="status">{html.escape(decision.status)}</span></td>'
			f'<td>{html.escape(comment)}</td><td>{html.escape("; ".join(decision.glossary_terms) or "—")}</td>'
			f'<td>{html.escape(decision.confidence)}</td></tr>'
		)
	conflict_rows = "".join(
		f"<tr><td>{html.escape(source_term)}</td><td>{html.escape(json.dumps(values, ensure_ascii=False))}</td></tr>"
		for source_term, values in sorted(glossary.conflicts.items())
	)
	document = f"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Rapport de traduction gettext</title><style>
:root{{--ink:#172033;--muted:#667085;--line:#d0d5dd;--bg:#f6f8fb;--good:#067647;--warn:#b54708;--bad:#b42318;--blue:#175cd3}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.45 system-ui,sans-serif}}main{{max-width:1600px;margin:auto;padding:28px}}
h1{{margin-bottom:4px}}.meta{{color:var(--muted)}}.summary{{display:flex;flex-wrap:wrap;gap:12px;margin:24px 0}}.card{{background:white;border:1px solid var(--line);border-radius:10px;padding:14px 20px;min-width:130px}}.card b{{display:block;font-size:24px}}.card span{{color:var(--muted)}}
.table-wrap{{overflow:auto;background:white;border:1px solid var(--line);border-radius:10px}}table{{border-collapse:collapse;width:100%;table-layout:fixed}}th,td{{border-bottom:1px solid #eaecf0;padding:10px;text-align:left;vertical-align:top;white-space:pre-wrap;overflow-wrap:anywhere}}th{{position:sticky;top:0;background:#f2f4f7;z-index:1}}th:nth-child(1){{width:16%}}th:nth-child(2){{width:9%}}th:nth-child(3),th:nth-child(4){{width:14%}}th:nth-child(5){{width:10%}}th:nth-child(6){{width:20%}}
.status{{font-weight:650}}tr.added .status{{color:var(--good)}}tr.manual .status{{color:var(--warn)}}tr.error .status{{color:var(--bad)}}tr.ignored .status{{color:var(--muted)}}tr.conserved .status{{color:var(--blue)}}details{{margin-top:24px}}code{{background:#eef2f6;padding:2px 4px;border-radius:4px}}
</style></head><body><main><h1>Rapport de traduction gettext</h1>
<p class="meta">Exécution : {html.escape(datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))} · Source : <code>{html.escape(str(source))}</code> · Mode : {"simulation" if dry_run else "écriture"}</p>
<section class="summary">{cards}</section><div class="table-wrap"><table><thead><tr><th>Clé source (msgid)</th><th>Contexte (msgctxt)</th><th>Ancienne traduction</th><th>Nouvelle traduction</th><th>Statut</th><th>Commentaire</th><th>Termes du glossaire utilisés</th><th>Confiance</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<details><summary>Conflits terminologiques ({len(glossary.conflicts)})</summary><div class="table-wrap"><table><thead><tr><th>Terme source</th><th>Variantes observées et fréquences</th></tr></thead><tbody>{conflict_rows}</tbody></table></div></details>
</main></body></html>"""
	path.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
		handle.write(document)
		temp_path = Path(handle.name)
	os.replace(temp_path, path)


def validate_with_msgfmt(path: Path) -> None:
	msgfmt = shutil.which("msgfmt")
	if not msgfmt:
		return
	result = subprocess.run([msgfmt, "--check", "--check-format", "-o", os.devnull, str(path)], capture_output=True, text=True)
	if result.returncode:
		raise RuntimeError(f"msgfmt a rejeté le fichier:\n{result.stderr.strip()}")


def build_parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("po_file", nargs="?", type=Path, default=Path("frappe/locale/fr.po"))
	parser.add_argument("--model", default=DEFAULT_MODEL)
	parser.add_argument("--report", type=Path, default=Path("translation-report.html"))
	parser.add_argument("--backup", type=Path, help="chemin de sauvegarde (défaut: <fichier>.bak-<date>)")
	parser.add_argument("--checkpoint", type=Path, default=Path(".translation-checkpoint.json"))
	parser.add_argument("--glossary", type=Path, help="glossaire JSON externe {anglais: français}")
	parser.add_argument("--source-root", type=Path, default=Path.cwd())
	parser.add_argument("--batch-size", type=int, default=12)
	parser.add_argument("--limit", type=int, help="limiter le nombre d'entrées envoyées (tests/revue progressive)")
	parser.add_argument("--dry-run", action="store_true", help="proposer et rapporter sans modifier le PO")
	parser.add_argument("--timeout", type=float, default=180)
	parser.add_argument("--retries", type=int, default=3)
	parser.add_argument("--api-key-env", default="OPENAI_API_KEY")
	return parser


def main(argv: list[str] | None = None) -> int:
	args = build_parser().parse_args(argv)
	if args.batch_size < 1 or (args.limit is not None and args.limit < 1):
		raise SystemExit("--batch-size et --limit doivent être positifs")
	po_path = args.po_file.resolve()
	original_text = po_path.read_text(encoding="utf-8")
	lines, entries = parse_po(original_text)
	glossary = Glossary(entries, args.glossary)
	decisions, candidates = baseline_decisions(entries)
	if args.limit:
		deferred, candidates = candidates[args.limit :], candidates[: args.limit]
		for entry in deferred:
			decisions.append(Decision(entry, old_values(entry), {}, "validation manuelle requise", "Entrée différée par l'option --limit.", confidence="faible"))
	checkpoint = load_checkpoint(args.checkpoint)
	checkpoint_changed = False
	for entry in candidates:
		if entry.key not in checkpoint:
			continue
		enriched = enrich_checkpoint_record(entry, checkpoint[entry.key])
		if enriched != checkpoint[entry.key]:
			checkpoint[entry.key] = enriched
			checkpoint_changed = True
	if checkpoint_changed:
		save_json_atomic(args.checkpoint, checkpoint)
	missing_api_candidates = [entry for entry in candidates if entry.key not in checkpoint]
	if missing_api_candidates:
		api_key = os.environ.get(args.api_key_env)
		if not api_key:
			raise SystemExit(f"la variable {args.api_key_env} est requise pour traduire {len(missing_api_candidates)} entrée(s)")
		client = OpenAIClient(api_key, args.model, args.timeout, args.retries)
		for offset in range(0, len(missing_api_candidates), args.batch_size):
			batch = missing_api_candidates[offset : offset + args.batch_size]
			payloads = [candidate_payload(entry, entries, glossary, args.source_root) for entry in batch]
			results = client.translate(payloads)
			for entry in batch:
				result = results.get(entry.key, {"id": entry.key, "status": "manual", "translations": [], "explanation": "Résultat absent du lot API.", "glossary_terms": [], "confidence": "low"})
				checkpoint[entry.key] = enrich_checkpoint_record(entry, result)
			save_json_atomic(args.checkpoint, checkpoint)
			print(f"Lot {offset // args.batch_size + 1}: {min(offset + len(batch), len(missing_api_candidates))}/{len(missing_api_candidates)} nouvelle(s) entrée(s)", file=sys.stderr)
	for entry in candidates:
		decisions.append(decide(entry, checkpoint[entry.key]))
	decisions.sort(key=lambda item: item.entry.ordinal)
	updated_text = apply_decisions(lines, decisions)
	_, updated_entries = parse_po(updated_text)
	assert_existing_preserved(entries, updated_entries)
	if not args.dry_run and updated_text != original_text:
		backup = args.backup or po_path.with_name(f"{po_path.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
		shutil.copy2(po_path, backup)
		with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=po_path.parent, delete=False) as handle:
			handle.write(updated_text)
			temp_path = Path(handle.name)
		try:
			validate_with_msgfmt(temp_path)
			os.chmod(temp_path, stat.S_IMODE(po_path.stat().st_mode))
			os.replace(temp_path, po_path)
		except Exception:
			temp_path.unlink(missing_ok=True)
			raise
		print(f"Sauvegarde: {backup}")
	try:
		render_report(args.report, po_path, decisions, glossary, args.dry_run)
	except Exception as exc:
		fallback = args.report.with_suffix(args.report.suffix + ".data.json")
		save_json_atomic(fallback, [{"msgid": item.entry.msgid, "status": item.status, "old": item.old, "new": item.new, "comment": item.comment, "errors": item.errors} for item in decisions])
		print(f"Erreur de génération HTML: {exc}. Données conservées dans {fallback}", file=sys.stderr)
		return 2
	summary = counts(decisions, len(glossary.conflicts))
	print(json.dumps(summary, ensure_ascii=False, indent=2))
	print(f"Rapport HTML: {args.report.resolve()}")
	return 0 if summary["errors"] == 0 else 1


if __name__ == "__main__":
	raise SystemExit(main())
