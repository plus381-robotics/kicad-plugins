"""Writing an imported part into the library."""

import unittest

from common import Case
from plus381 import importer

SYMBOL = '''(symbol "LM358"
		(property "Reference" "U"
			(at 0 0 0)
		)
		(property "Value" "LM358"
			(at 0 0 0)
		)
		(symbol "LM358_1_1"
			(pin input line (at 0 0 0) (length 2.54) (name "IN") (number "1"))
		)
	)'''
HEADER = '(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "test")\n'
FOOTPRINT = '''(footprint "SOIC-8"
	(layer "F.Cu")
	(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))
	(model "${VENDOR}/SOIC-8.step"
		(offset (xyz 0 0 0))
		(scale (xyz 1 1 1))
		(rotate (xyz 0 0 90))
	)
)
'''


class Library(Case):
    def setUp(self):
        super().setUp()
        self.parts = self.tmp / "parts"
        for d in ("symbols/Plus381_ICs.kicad_symdir", "symbols/Plus381_Misc.kicad_symdir",
                  "footprints/Plus381_ICs.pretty"):
            (self.parts / d).mkdir(parents=True)
        (self.parts / "categories.json").write_text('{"prefix": "Plus381_", "categories": {"ICs": "", "Misc": ""}}')
        self.model = self.tmp / "SOIC-8.step"
        self.model.write_text("ISO-10303-21;")
        self.patch(importer, "find_kicad_cli", lambda: None)

    def choices(self, **kw):
        c = importer.Choices(category="ICs", symbol=importer.Symbol("LM358", SYMBOL, HEADER), symbol_name="LM358",
                             footprint=importer.Footprint("SOIC-8", FOOTPRINT), footprint_name="SOIC-8",
                             model=self.model, rotation=[0.0, 0.0, 90.0], mpn="LM358DR", manufacturer="TI")
        for k, v in kw.items():
            setattr(c, k, v)
        return c

    def files(self):
        return sorted(p.relative_to(self.parts).as_posix() for p in self.parts.rglob("*")
                      if p.is_file() and p.name != "categories.json")

    def test_import_writes_symbol_footprint_and_model(self):
        c = self.choices()
        self.assertEqual(importer.check_choices(self.parts, c), [])
        importer.do_import(self.parts, c)
        self.assertEqual(self.files(), ["3dmodels/SOIC-8.step", "footprints/Plus381_ICs.pretty/SOIC-8.kicad_mod",
                                        "symbols/Plus381_ICs.kicad_symdir/LM358.kicad_sym"])
        sym = (self.parts / "symbols/Plus381_ICs.kicad_symdir/LM358.kicad_sym").read_text()
        self.assertEqual(importer.get_prop(sym, "Footprint"), "Plus381_ICs:SOIC-8")
        self.assertEqual(importer.get_prop(sym, "MPN"), "LM358DR")
        fp = (self.parts / "footprints/Plus381_ICs.pretty/SOIC-8.kicad_mod").read_text()
        self.assertIn('(model "${PLUS381_PARTS}/3dmodels/SOIC-8.step"', fp)
        self.assertNotIn("VENDOR", fp)

    def test_existing_part_is_found_whatever_its_name_looks_like(self):
        d = self.parts / "symbols/Plus381_Misc.kicad_symdir"
        (d / "SOT-23[5].kicad_sym").write_text("x")   # [ ] used to be read as a search pattern
        (d / "lm358.kicad_sym").write_text("x")       # same file as LM358 on Windows and macOS
        self.assertEqual(importer.find_existing(self.parts, "sym", "SOT-23[5]"), ["Plus381_Misc:SOT-23[5]"])
        self.assertEqual(importer.find_existing(self.parts, "sym", "SOT-23*"), [])
        errors = importer.check_choices(self.parts, self.choices())
        self.assertEqual(errors, ["Symbol Plus381_Misc:lm358 already exists (tick 'Replace existing')."])

    def test_replace_removes_the_copy_in_another_category(self):
        old = self.parts / "symbols/Plus381_Misc.kicad_symdir/lm358.kicad_sym"
        old.write_text("x")
        importer.do_import(self.parts, self.choices(replace=True))
        self.assertFalse(old.exists())
        self.assertTrue((self.parts / "symbols/Plus381_ICs.kicad_symdir/LM358.kicad_sym").exists())

    def test_failed_import_leaves_the_library_as_it_was(self):
        old_sym = self.parts / "symbols/Plus381_Misc.kicad_symdir/LM358.kicad_sym"
        old_sym.write_text("old symbol")
        old_fp = self.parts / "footprints/Plus381_ICs.pretty/SOIC-8.kicad_mod"
        old_fp.write_text("old footprint")
        before = self.files()
        # KiCad rejects the converted symbol (the last step).
        self.patch(importer, "find_kicad_cli", lambda: "kicad-cli")
        self.patch(importer, "_normalise_footprint", lambda path: None)
        self.patch(importer, "kicad_cli", lambda *args: False)
        with self.assertRaises(RuntimeError):
            importer.do_import(self.parts, self.choices(replace=True))
        self.assertEqual(self.files(), before)
        self.assertEqual(old_sym.read_text(), "old symbol")
        self.assertEqual(old_fp.read_text(), "old footprint")

    def test_identical_model_is_reused_and_survives_a_failed_import(self):
        (self.parts / "3dmodels").mkdir()
        (self.parts / "3dmodels/Shared.step").write_text("ISO-10303-21;")
        self.patch(importer, "find_kicad_cli", lambda: "kicad-cli")
        self.patch(importer, "_normalise_footprint", lambda path: None)
        self.patch(importer, "kicad_cli", lambda *args: False)
        with self.assertRaises(RuntimeError):
            importer.do_import(self.parts, self.choices())
        self.assertEqual(self.files(), ["3dmodels/Shared.step"])

    def test_broken_download_leaves_no_temporary_folder(self):
        import tempfile
        self.patch(tempfile, "tempdir", str(self.tmp))
        bad = self.tmp / "bad.zip"
        bad.write_text("not a zip")
        with self.assertRaises(Exception):
            importer.analyze(bad)
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.startswith("plus381-")], [])


if __name__ == "__main__":
    unittest.main()
