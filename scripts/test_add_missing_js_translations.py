import unittest

from add_missing_js_translations import MessageKey, extract_messages, render_entries, validate_po


class AddMissingJsTranslationsTests(unittest.TestCase):
	def test_extracts_direct_and_indirect_messages(self):
		code = '''
const unrelated = { method: "GET" };
const item = {
    filter_name: "Save Current Filter",
};
const translated = __(item.filter_name);
const direct = __("Create Saved Filter");
const contextual = __("Create a new {0}", null, "List shortcut");
'''
		messages = extract_messages(code)
		values = {key.msgid for _line, key, _kind in messages}
		self.assertEqual(values, {"Save Current Filter", "Create Saved Filter", "Create a new {0}"})
		self.assertIn(MessageKey("Create a new {0}", "List shortcut"), {key for _line, key, _kind in messages})

	def test_supports_variable_and_bracket_property_assignments(self):
		code = '''
let status = "Open";
const first = __(status);
row["caption"] = "Closed";
const second = __(row["caption"]);
'''
		values = {key.msgid for _line, key, _kind in extract_messages(code)}
		self.assertEqual(values, {"Open", "Closed"})

	def test_rendered_entries_form_a_valid_catalog(self):
		discovered = {
			MessageKey('Save "Current" Filter'): {
				("frappe/public/list.js", 10, "indirect via label"),
			}
		}
		contents = 'msgid ""\nmsgstr ""\n"Language: fr\\n"\n\n' + render_entries(discovered)
		validate_po(contents)
		self.assertIn(r'msgid "Save \"Current\" Filter"', contents)


if __name__ == "__main__":
	unittest.main()
