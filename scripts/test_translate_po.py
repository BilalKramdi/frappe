import tempfile
import unittest
from pathlib import Path

from translate_po import (
	Decision,
	Glossary,
	apply_decisions,
	assert_existing_preserved,
	baseline_decisions,
	enrich_checkpoint_record,
	normalize_checkpoint_result,
	parse_po,
	render_report,
	validate_translation,
)

SAMPLE = '''msgid ""
msgstr ""
"Language: fr\\n"

#. Button label
#: app/form.js:10
msgctxt "Button text"
msgid "Save {name}"
msgstr ""

msgid "Company"
msgstr "Société"

#~ msgid "Old"
#~ msgstr ""
'''


class TranslatePOTests(unittest.TestCase):
	def test_lossless_replacement_and_preservation(self):
		lines, entries = parse_po(SAMPLE)
		self.assertEqual(len(entries), 4)
		decisions, candidates = baseline_decisions(entries)
		self.assertEqual([entry.msgid for entry in candidates], ["Save {name}"])
		self.assertEqual(sum(item.status == "entrée ignorée" for item in decisions), 2)
		candidate = candidates[0]
		decisions.append(Decision(candidate, {"msgstr": ""}, {"msgstr": "Enregistrer {name}"}, "traduction ajoutée", "Bouton."))
		updated = apply_decisions(lines, decisions)
		self.assertIn('msgstr "Enregistrer {name}"', updated)
		self.assertIn('msgstr "Société"', updated)
		_, after = parse_po(updated)
		assert_existing_preserved(entries, after)

	def test_placeholder_validation(self):
		self.assertEqual(validate_translation("Hello {name} %s", "Bonjour {name} %s"), [])
		self.assertEqual(validate_translation("Use % for any value", "Utiliser % pour toute valeur"), [])
		self.assertEqual(validate_translation("Value: %1$s", "Valeur : %1$s"), [])
		self.assertTrue(validate_translation("Hello {name} %s", "Bonjour %s"))
		self.assertTrue(validate_translation("<b>{x}</b>", "<strong>{x}</strong>"))

	def test_absent_msgstr_is_inserted(self):
		text = 'msgid "Missing"\n\n'
		lines, entries = parse_po(text)
		decisions, candidates = baseline_decisions(entries)
		self.assertEqual(len(candidates), 1)
		entry = candidates[0]
		decisions.append(Decision(entry, {"msgstr": ""}, {"msgstr": "Manquant"}, "traduction ajoutée", "Libellé."))
		self.assertEqual(apply_decisions(lines, decisions), 'msgid "Missing"\nmsgstr "Manquant"\n\n')

	def test_checkpoint_contains_msgid(self):
		_, entries = parse_po(SAMPLE)
		entry = next(item for item in entries if item.msgid == "Save {name}")
		record = enrich_checkpoint_record(entry, {"id": entry.key, "translations": []})
		self.assertEqual(record["msgid"], "Save {name}")
		self.assertEqual(record["old_translations"], {"msgstr": ""})

	def test_corrupt_non_breaking_space_is_repaired(self):
		result = {"translations": [{"field": "msgstr", "value": "Élément\x00a0?"}]}
		normalized = normalize_checkpoint_result(result)
		self.assertEqual(normalized["translations"][0]["value"], "Élément\u00a0?")
		self.assertTrue(validate_translation("Item?", "Élément\x00?"))

	def test_glossary_and_safe_html_report(self):
		_, entries = parse_po(SAMPLE)
		glossary = Glossary(entries)
		self.assertEqual(glossary.by_source["Company"].most_common(1)[0][0], "Société")
		decisions, _ = baseline_decisions(entries)
		with tempfile.TemporaryDirectory() as directory:
			path = Path(directory) / "report.html"
			render_report(path, Path("fr.po"), decisions, glossary, True)
			contents = path.read_text(encoding="utf-8")
			self.assertIn("Rapport de traduction", contents)
			self.assertNotIn("<b>{x}</b>", contents)


if __name__ == "__main__":
	unittest.main()
