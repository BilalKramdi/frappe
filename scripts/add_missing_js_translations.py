#!/usr/bin/env python3
"""Add JavaScript translation strings missing from a gettext PO catalog.

Besides regular ``__("literal")`` calls, this script detects a conservative
subset of indirect calls such as::

    const item = { label: "Save Current Filter" };
    __(item.label)

Only properties or variables passed to ``__()`` in the same file are
considered. This deliberately avoids treating every JavaScript string as UI
text.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

IGNORED_DIRECTORY_NAMES = {".git", "node_modules", "dist", "coverage"}
JS_STRING_PATTERN = r'''(?:"(?:\\.|[^"\\\r\n])*"|'(?:\\.|[^'\\\r\n])*'|`(?:\\.|[^`\\$]|\$(?!\{))*`)'''
DYNAMIC_CALL_RE = re.compile(
	r"(?<![\w$])__\s*\(\s*"
	r"(?P<expression>[A-Za-z_$][\w$]*(?:(?:\?\.|\.)[A-Za-z_$][\w$]*|\[\s*['\"][^'\"]+['\"]\s*\])*)"
	r"\s*(?:,|\))"
)


@dataclass(frozen=True)
class MessageKey:
	msgid: str
	context: str | None = None


def message_sort_key(key: MessageKey) -> tuple[str, str]:
	return key.msgid, key.context or ""


def decode_javascript_string(raw: str) -> str:
	"""Decode the common JavaScript escapes used in static string literals."""
	body = raw[1:-1]
	result: list[str] = []
	i = 0
	simple_escapes = {
		"b": "\b",
		"f": "\f",
		"n": "\n",
		"r": "\r",
		"t": "\t",
		"v": "\v",
		"0": "\0",
	}
	while i < len(body):
		if body[i] != "\\":
			result.append(body[i])
			i += 1
			continue

		i += 1
		if i >= len(body):
			result.append("\\")
			break
		escaped = body[i]
		if escaped in simple_escapes:
			result.append(simple_escapes[escaped])
			i += 1
		elif escaped == "x" and re.fullmatch(r"[0-9A-Fa-f]{2}", body[i + 1 : i + 3]):
			result.append(chr(int(body[i + 1 : i + 3], 16)))
			i += 3
		elif escaped == "u" and re.fullmatch(r"[0-9A-Fa-f]{4}", body[i + 1 : i + 5]):
			result.append(chr(int(body[i + 1 : i + 5], 16)))
			i += 5
		elif escaped == "\n":
			i += 1
		elif escaped == "\r":
			i += 2 if body[i + 1 : i + 2] == "\n" else 1
		else:
			result.append(escaped)
			i += 1
	return "".join(result)


def javascript_code_mask(code: str) -> tuple[str, dict[int, tuple[int, str]]]:
	"""Return code with comments/string contents masked and static strings indexed.

	JavaScript inside ``${...}`` remains visible, so translation calls embedded
	in template literals are still detected.
	"""
	mask = [" "] * len(code)
	strings: dict[int, tuple[int, str]] = {}

	def scan_quoted(start: int, quote: str) -> int:
		i = start + 1
		while i < len(code):
			if code[i] == "\\":
				i += 2
				continue
			if code[i] == quote:
				i += 1
				strings[start] = (i, decode_javascript_string(code[start:i]))
				return i
			i += 1
		return i

	def scan_template(start: int) -> int:
		i = start + 1
		has_expression = False
		while i < len(code):
			if code[i] == "\\":
				i += 2
				continue
			if code.startswith("${", i):
				has_expression = True
				mask[i] = "$"
				mask[i + 1] = "{"
				i = scan_code(i + 2, stop_at_closing_brace=True)
				continue
			if code[i] == "`":
				i += 1
				if not has_expression:
					strings[start] = (i, decode_javascript_string(code[start:i]))
				return i
			i += 1
		return i

	def scan_code(start: int, stop_at_closing_brace: bool = False) -> int:
		i = start
		brace_depth = 0
		while i < len(code):
			if code.startswith("//", i):
				newline = code.find("\n", i + 2)
				i = len(code) if newline < 0 else newline
				continue
			if code.startswith("/*", i):
				end = code.find("*/", i + 2)
				i = len(code) if end < 0 else end + 2
				continue
			char = code[i]
			if char in {'"', "'"}:
				i = scan_quoted(i, char)
				continue
			if char == "`":
				i = scan_template(i)
				continue
			if stop_at_closing_brace:
				if char == "{":
					brace_depth += 1
				elif char == "}":
					if brace_depth == 0:
						mask[i] = char
						return i + 1
					brace_depth -= 1
			mask[i] = char
			i += 1
		return i

	scan_code(0)
	return "".join(mask), strings


def final_expression_name(expression: str) -> str:
	bracket_name = re.search(r"\[\s*['\"]([^'\"]+)['\"]\s*\]\s*$", expression)
	if bracket_name:
		return bracket_name.group(1)
	return re.split(r"\?\.|\.", expression)[-1]


def literal_assignments(code: str, code_mask: str, name: str) -> list[tuple[int, str]]:
	"""Find literal values assigned to a translated property or variable."""
	escaped_name = re.escape(name)
	patterns = [
		# Object properties: { label: "Text" } or { "label": "Text" }
		re.compile(
			rf"(?<![\w$])(?:{escaped_name}|['\"]{escaped_name}['\"])\s*:\s*"
			rf"(?P<string>{JS_STRING_PATTERN})"
		),
		# Variable declarations and direct assignments: const label = "Text"
		re.compile(
			rf"(?<![.\w$])(?:const\s+|let\s+|var\s+)?{escaped_name}\s*=\s*"
			rf"(?P<string>{JS_STRING_PATTERN})"
		),
		# Property assignments: item.label = "Text"
		re.compile(
			rf"(?:\.{escaped_name}|\[\s*['\"]{escaped_name}['\"]\s*\])\s*=\s*"
			rf"(?P<string>{JS_STRING_PATTERN})"
		),
	]

	seen: set[tuple[int, str]] = set()
	for pattern in patterns:
		for match in pattern.finditer(code):
			if not code_mask[match.start() : match.start() + 1].strip():
				continue
			raw = match.group("string")
			value = decode_javascript_string(raw)
			if not value or "${" in value or re.search(r"['\"]\s*\+\s*", value):
				continue
			line = code.count("\n", 0, match.start("string")) + 1
			seen.add((line, value))
	return sorted(seen)


def extract_messages(code: str) -> list[tuple[int, MessageKey, str]]:
	"""Return direct and conservatively inferred indirect translations."""
	results: set[tuple[int, MessageKey, str]] = set()
	code_mask, static_strings = javascript_code_mask(code)
	dynamic_matches = [
		match for match in DYNAMIC_CALL_RE.finditer(code) if code_mask[match.start() : match.start() + 1].strip()
	]
	dynamic_names = {final_expression_name(match.group("expression")) for match in dynamic_matches}
	# A direct call is an unmasked __ followed by a static string token.
	for match in re.finditer(r"(?<![\w$])__\s*\(", code_mask):
		string_start = match.end()
		while string_start < len(code) and code[string_start].isspace():
			string_start += 1
		string_token = static_strings.get(string_start)
		if string_token:
			string_end, msgid = string_token
			context = None
			context_prefix = re.match(r"\s*,\s*(?:null|undefined)\s*,\s*", code[string_end:])
			if context_prefix:
				context_start = string_end + context_prefix.end()
				context_token = static_strings.get(context_start)
				if context_token:
					_context_end, context = context_token
			line = code.count("\n", 0, string_start) + 1
			results.add((line, MessageKey(msgid, context), "direct"))

	for name in dynamic_names:
		for line, value in literal_assignments(code, code_mask, name):
			results.add((line, MessageKey(value), f"indirect via {name}"))

	return sorted(results, key=lambda item: (item[0], *message_sort_key(item[1]), item[2]))


def javascript_files(source: Path):
	for path in source.rglob("*.js"):
		if any(part in IGNORED_DIRECTORY_NAMES for part in path.parts):
			continue
		if path.name.endswith(".min.js"):
			continue
		yield path


def existing_messages(po_path: Path) -> set[MessageKey]:
	return parse_po_messages(po_path.read_text(encoding="utf-8"))


def parse_po_messages(contents: str) -> set[MessageKey]:
	"""Parse message keys using only the Python standard library."""
	messages: set[MessageKey] = set()
	context: str | None = None
	msgid: str | None = None
	active_field: str | None = None
	obsolete = False

	def finish_entry() -> None:
		nonlocal context, msgid, active_field, obsolete
		if msgid and not obsolete:
			messages.add(MessageKey(msgid, context))
		context = None
		msgid = None
		active_field = None
		obsolete = False

	for line_number, line in enumerate(contents.splitlines(), start=1):
		if not line.strip():
			finish_entry()
			continue
		if line.startswith("#~"):
			obsolete = True
			continue
		field_match = re.match(r'^(msgctxt|msgid)\s+(".*")\s*$', line)
		if field_match:
			active_field = field_match.group(1)
			try:
				value = json.loads(field_match.group(2))
			except json.JSONDecodeError as exc:
				raise ValueError(f"Invalid PO string on line {line_number}: {exc}") from exc
			if active_field == "msgctxt":
				context = value
			else:
				msgid = value
			continue
		continuation = re.match(r'^(".*")\s*$', line)
		if continuation and active_field:
			try:
				value = json.loads(continuation.group(1))
			except json.JSONDecodeError as exc:
				raise ValueError(f"Invalid PO string on line {line_number}: {exc}") from exc
			if active_field == "msgctxt":
				context = (context or "") + value
			elif active_field == "msgid":
				msgid = (msgid or "") + value

	finish_entry()
	return messages


def po_quote(value: str) -> str:
	# PO quoted strings use the same escaping needed here as JSON strings.
	return json.dumps(value, ensure_ascii=False)


def render_entries(discovered: dict[MessageKey, set[tuple[str, int, str]]]) -> str:
	blocks: list[str] = []
	for key in sorted(discovered, key=message_sort_key):
		references = sorted({f"{path}:{line}" for path, line, _kind in discovered[key]})
		kinds = sorted({kind for _path, _line, kind in discovered[key]})
		lines = [
			f"#. Added by add_missing_js_translations.py ({', '.join(kinds)})",
			f"#: {' '.join(references)}",
		]
		if key.context is not None:
			lines.append(f"msgctxt {po_quote(key.context)}")
		lines.extend((f"msgid {po_quote(key.msgid)}", 'msgstr ""'))
		blocks.append("\n".join(lines))
	return "\n\n".join(blocks) + "\n"


def validate_po(contents: str) -> None:
	parse_po_messages(contents)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--source", type=Path, default=Path("frappe"), help="directory containing JS files")
	parser.add_argument("--po", type=Path, default=Path("frappe/locale/fr.po"), help="PO catalog to update")
	parser.add_argument("--dry-run", action="store_true", help="show missing strings without changing the PO file")
	parser.add_argument("--verbose", action="store_true", help="show every discovered missing string")
	return parser.parse_args()


def main() -> int:
	args = parse_args()
	if not args.source.is_dir():
		raise SystemExit(f"Source directory does not exist: {args.source}")
	if not args.po.is_file():
		raise SystemExit(f"PO catalog does not exist: {args.po}")

	existing = existing_messages(args.po)
	discovered: dict[MessageKey, set[tuple[str, int, str]]] = defaultdict(set)
	file_count = 0
	for js_path in javascript_files(args.source):
		file_count += 1
		try:
			code = js_path.read_text(encoding="utf-8")
		except UnicodeDecodeError:
			print(f"Warning: skipped non-UTF-8 file {js_path}", file=sys.stderr)
			continue
		reference_path = js_path.as_posix()
		for line, key, kind in extract_messages(code):
			if key not in existing:
				discovered[key].add((reference_path, line, kind))

	print(f"Scanned {file_count} JavaScript files; found {len(discovered)} missing PO entries.")
	if args.verbose or args.dry_run:
		for key in sorted(discovered, key=message_sort_key):
			context = f" [context: {key.context}]" if key.context else ""
			print(f"- {key.msgid}{context}")

	if not discovered or args.dry_run:
		return 0

	original = args.po.read_text(encoding="utf-8")
	separator = "" if original.endswith("\n\n") else ("\n" if original.endswith("\n") else "\n\n")
	updated = original + separator + render_entries(discovered)
	validate_po(updated)
	args.po.write_text(updated, encoding="utf-8")
	print(f"Updated {args.po}. New msgstr values are empty and still need French translations.")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
