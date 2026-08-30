from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config

from app.database import Database
from app.form_fill import build_form_fill_script, form_fill_values
from app.pdf_export import render_cover_letter_pdf, render_resume_pdf
from app.docx_export import render_resume_docx
from app.secrets import protect_secret, unprotect_secret
from app.services import JobService, ResumeService, SupportingDocumentService, compact_job_text, evaluate_requirement, extract_job_facts, generation_instruction, weighted_match_score
from app.ai_manager import AIError, validated_endpoint
from app.validators import recommendation, validate_job_analysis, validate_resume_changes


class DummyAI:
    pass


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "test.db")

    def tearDown(self): self.tmp.cleanup()

    def test_job_deduplication(self):
        job = {"company":"ABC", "title":"Engineer", "location":"Ottawa", "url":"https://example.com/1", "description":"CAD", "description_hash":"x"}
        first, new1 = self.db.upsert_job(job); second, new2 = self.db.upsert_job(job)
        self.assertEqual(first, second); self.assertTrue(new1); self.assertFalse(new2)

    def test_duplicate_job_keeps_known_salary_when_new_scrape_is_blank(self):
        job = {"company":"ABC", "title":"Engineer", "location":"Ottawa", "url":"https://example.com/salary", "description":"First version", "description_hash":"one", "salary":"$70,000 - $80,000"}
        job_id, _ = self.db.upsert_job(job)
        job.update({"description":"Updated version", "description_hash":"two", "salary":""})
        self.db.upsert_job(job)
        self.assertEqual(self.db.job(job_id)["salary"], "$70,000 - $80,000")

    def test_unmark_applied_restores_the_previous_status(self):
        job_id, _ = self.db.upsert_job({"company":"ABC", "title":"Engineer", "location":"Ottawa", "url":"https://example.com/applied", "description":"", "description_hash":"applied"})
        self.db.update_job(job_id, status="Ready")
        self.db.mark_applied(job_id)
        self.assertEqual(self.db.job(job_id)["status"], "Applied")
        self.assertTrue(self.db.unmark_applied(job_id))
        self.assertEqual(self.db.job(job_id)["status"], "Ready")

    def test_remove_draft_records(self):
        job_id, _ = self.db.upsert_job({"company":"ABC", "title":"Engineer", "location":"Ottawa", "url":"https://example.com/draft", "description":"", "description_hash":"draft"})
        version_id = self.db.add_resume_version(job_id=job_id, version_name="resume_v001", content="draft", document_type="Resume")
        self.assertEqual(self.db.remove_resume_version(version_id)["version_name"], "resume_v001")
        cover_id = self.db.add_cover_letter(job_id, "C:/temp/cover.pdf", "draft", "test")
        self.assertEqual(self.db.remove_cover_letter(cover_id)["id"], cover_id)

    def test_replace_resume_draft_keeps_the_selected_record(self):
        job_id, _ = self.db.upsert_job({"company":"ABC", "title":"Engineer", "location":"Ottawa", "url":"https://example.com/replace", "description":"", "description_hash":"replace"})
        version_id = self.db.add_resume_version(job_id=job_id, version_name="cv_v001", content="old", document_type="CV")
        self.db.set_resume_decision(version_id, True)
        self.db.replace_resume_version(version_id, job_id=job_id, content="new", changes_json="[]", document_type="CV", model_used="test")
        record = next(version for version in self.db.resume_versions() if version["id"] == version_id)
        self.assertEqual(record["version_name"], "cv_v001")
        self.assertEqual(record["content"], "new")
        self.assertFalse(record["approved"])

    def test_csv_import_and_deduplication(self):
        source = Path(self.tmp.name) / "jobs.csv"
        source.write_text("company,title,location,url,description\nABC,Engineer,Ottawa,https://example.com/2,CAD testing\n", encoding="utf-8")
        service = JobService(self.db, DummyAI())
        first = service.import_file(str(source)); second = service.import_file(str(source))
        self.assertEqual(first["added"], 1); self.assertEqual(second["existing"], 1)

    def test_no_resume_clears_legacy_query_derived_scores(self):
        job_id, _ = self.db.upsert_job({"company":"ABC", "title":"Engineer", "location":"Ottawa", "url":"https://example.com/no-resume", "description":"Mechanical engineering", "description_hash":"no-resume"})
        self.db.update_job(job_id, rule_score=55, ai_score=50, match_score=54)
        with patch.object(JobService, "_resume_text", return_value=""):
            JobService(self.db, DummyAI())
        job = self.db.job(job_id)
        self.assertIsNone(job["rule_score"]); self.assertIsNone(job["ai_score"]); self.assertIsNone(job["match_score"])

    def test_analyze_requires_candidate_resume_evidence(self):
        job_id, _ = self.db.upsert_job({"company":"ABC", "title":"Engineer", "location":"Ottawa", "url":"https://example.com/analyze-empty", "description":"CAD", "description_hash":"analyze-empty"})
        service = JobService(self.db, DummyAI())
        with self.assertRaisesRegex(ValueError, "Import an original DOCX"):
            service.analyze(job_id, "")

    def test_recommendation_bands(self):
        self.assertEqual(recommendation(85), "EXCELLENT"); self.assertEqual(recommendation(75), "GOOD"); self.assertEqual(recommendation(60), "POSSIBLE"); self.assertEqual(recommendation(39), "POOR")

    def test_resume_new_number_is_high_risk(self):
        data = {"changes":[{"original":"Designed a wing", "suggested":"Designed 50 wings", "reason":"impact", "risk":"LOW"}]}
        checked = validate_resume_changes(data, "Designed a wing")
        self.assertEqual(checked["changes"][0]["risk"], "HIGH")

    def test_resume_new_tool_is_high_risk(self):
        data = {"changes":[{"original":"Designed components", "suggested":"Designed components in SolidWorks", "reason":"keyword", "risk":"LOW"}]}
        checked = validate_resume_changes(data, "Designed components")
        self.assertEqual(checked["changes"][0]["risk"], "HIGH")

    def test_analysis_validation(self):
        value = {"ai_score":82,"required_matches":[],"preferred_matches":[],"missing_skills":[],"strengths":[],"risks":[],"reason":"ok","recommendation":"GOOD"}
        self.assertEqual(validate_job_analysis(value)["ai_score"], 82)

    def test_job_analysis_payload_has_a_single_resume_label(self):
        class RecordingAI:
            def generate_json(self, _task, _prompt, payload, *_args):
                self.payload = payload
                return {"ai_score":50,"required_matches":[],"preferred_matches":[],"missing_skills":[],"strengths":[],"risks":[],"reason":"ok","recommendation":"POSSIBLE"}, "test"
        ai = RecordingAI(); service = JobService(self.db, ai)
        job_id, _ = self.db.upsert_job({"company":"ABC", "title":"Engineer", "location":"Ottawa", "url":"https://example.com/analysis", "description":"Python", "description_hash":"analysis"})
        service.analyze(job_id, "RESUME:\nJane Doe\nPython")
        self.assertTrue(ai.payload.startswith("RESUME:\nJane Doe")); self.assertNotIn("RESUME:\nRESUME:", ai.payload)

    def test_rule_score_penalizes_seniority(self):
        junior = JobService._rule_score("Python CAD engineering", "Python CAD engineering", "Ottawa")[0]
        senior = JobService._rule_score("Python CAD engineering", "Senior engineer, 10+ years, Python CAD engineering", "Ottawa")[0]
        self.assertGreater(junior, senior)

    def test_empty_resume_path_is_not_treated_as_the_current_directory(self):
        service = ResumeService(self.db, DummyAI())
        service.paths = type("P", (), {"resumes_original": Path(self.tmp.name) / "resumes"})()
        service.paths.resumes_original.mkdir()
        with patch("app.services.load_settings", return_value={"resume_path": ""}):
            self.assertIsNone(service.original_path())

    def test_job_fact_extraction(self):
        facts = extract_job_facts(
            "Mechanical Engineering Co-op - Starting Fall 2026",
            "About You\n* Bachelor's degree in Mechanical Engineering.\n* Minimum of 1 year experience.\n* Strong communication skills.\n* Must work onsite.\n* Valid driver's license required.\nBenefits\nHealth plan.",
        )
        self.assertEqual(facts["start_date"], "Fall 2026")
        self.assertIn("Bachelor's degree in Mechanical Engineering.", facts["education"])
        self.assertTrue(any("1 year" in item for item in facts["other_requirements"]))
        self.assertTrue(any("onsite" in item for item in facts["other_requirements"]))
        self.assertTrue(any("communication" in item for item in facts["other_requirements"]))

    def test_job_fact_extraction_does_not_guess_start_date(self):
        facts = extract_job_facts("Junior Engineer", "Engineering degree required. Posted yesterday.")
        self.assertEqual(facts["start_date"], "Not stated")

    def test_requirement_status_uses_verified_facts(self):
        candidate = "Bachelor of Mechanical and Aerospace Engineering. Languages: English, Chinese."
        self.assertEqual(evaluate_requirement("Bachelor's degree in Mechanical Engineering.", candidate), "LIKELY_MET")
        self.assertEqual(evaluate_requirement("Valid driver's license required.", candidate), "NOT_CONFIRMED")
        self.assertEqual(evaluate_requirement("French proficiency required.", candidate), "NOT_CONFIRMED")

    def test_job_text_compaction(self):
        source = "**Requirements**\n\n\n\n* Bachelor\\'s degree.\n   \n   \nParagraph with   spaces."
        compacted = compact_job_text(source)
        self.assertNotIn("**", compacted)
        self.assertNotIn("\n\n\n", compacted)
        self.assertIn("• Bachelor's degree.", compacted)

    def test_job_html_is_converted_without_markdownify(self):
        compacted = compact_job_text("<h1>Requirements</h1><ul><li>CAD &amp; CFD</li></ul>")
        self.assertIn("Requirements", compacted); self.assertIn("• CAD & CFD", compacted); self.assertNotIn("<h1>", compacted)

    def test_external_ai_requires_https_and_local_ai_requires_loopback(self):
        self.assertEqual(validated_endpoint("http://127.0.0.1:11434", local_only=True), "http://127.0.0.1:11434")
        self.assertEqual(validated_endpoint("https://api.example.com/v1"), "https://api.example.com/v1")
        with self.assertRaises(AIError): validated_endpoint("http://api.example.com/v1")
        with self.assertRaises(AIError): validated_endpoint("https://remote.example.com", local_only=True)

    def test_windows_api_key_protection_roundtrip(self):
        try:
            encrypted = protect_secret("test-key-not-real")
        except OSError as exc:
            self.skipTest(f"Windows DPAPI is unavailable to this sandbox account: {exc}")
        self.assertNotIn("test-key-not-real", encrypted)
        self.assertTrue(encrypted.startswith("dpapi:"))
        self.assertEqual(unprotect_secret(encrypted), "test-key-not-real")

    def test_form_fill_script_uses_only_confirmed_contact_values_and_never_submits(self):
        values = form_fill_values({"first_name": "Jane", "email": "jane@example.com", "skills": "CAD"})
        self.assertEqual(values, {"first_name": "Jane", "email": "jane@example.com"})
        script = build_form_fill_script({"first_name": "Jane", "email": "jane@example.com"})
        self.assertIn("jane@example.com", script)
        self.assertNotIn(".submit(", script)
        self.assertNotIn(".click(", script)

    def test_generation_instruction_is_optional_and_local(self):
        from unittest.mock import patch
        with patch("app.services.load_settings", return_value={"generation_prompts": {"resume": "Focus on CAD."}}):
            self.assertEqual(generation_instruction("resume"), "\n\nUSER-APPROVED EXTRA INSTRUCTIONS:\nFocus on CAD.")
            self.assertEqual(generation_instruction("cover_letter"), "")

    def test_match_score_uses_the_configured_rule_ai_split(self):
        self.assertEqual(weighted_match_score(80, 20, {"match_weights": {"rule": 50, "ai": 50}}), 50)
        self.assertEqual(weighted_match_score(80, 20, {"match_weights": {"rule": 100, "ai": 0}}), 80)

    def test_salary_falls_back_to_multiple_ranges_in_description(self):
        description = "Compensation AB: $22 - $32.25. MB & ON: $20.50 - $30.25. NB: $19.50 - $30.25."
        self.assertEqual(JobService._salary_from_description(description), "$19.5 - $32.25 (varies by location)")

    def test_resume_and_cv_draft_tabs_use_separate_preview_panels(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from app.gui import ResumePage
        app = QApplication.instance() or QApplication([])
        database = type("Db", (), {"resume_versions": lambda self: [], "cover_letters": lambda self: []})()
        service = type("Resume", (), {"original_text": lambda self: (_ for _ in ()).throw(FileNotFoundError()), "imported_source_path": lambda self: None})()
        page = ResumePage(database, service)
        page.document_lists.setCurrentIndex(1)
        page.show_document_category(1)
        self.assertIs(page.document_stack.currentWidget(), page.cover_panel)
        self.assertEqual(page.generate_draft_button.text(), "Generate CV Draft")
        page.document_lists.setCurrentIndex(0)
        self.assertIs(page.document_stack.currentWidget(), page.resume_panel)
        self.assertEqual(page.generate_draft_button.text(), "Generate Resume Draft")
        self.assertFalse(hasattr(page, "generate_resume_button"))
        self.assertFalse(hasattr(page, "generate_cv_button"))

    def test_application_rows_show_explicit_start_and_post_dates(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from app.gui import ApplicationsPage
        app = QApplication.instance() or QApplication([])
        self.db.upsert_job({"company":"ABC", "title":"Engineering Co-op - Starting Fall 2027", "location":"Ottawa", "url":"https://example.com/dates", "description":"Posted role", "description_hash":"dates", "start_date":"Fall 2027", "date_posted":"2026-08-27T10:00:00"})
        page = ApplicationsPage(self.db)
        self.assertEqual(page.table.columnCount(), 8)
        self.assertEqual(page.table.item(0, 4).text(), "Fall 2027")
        self.assertEqual(page.table.item(0, 5).text(), "2026-08-27")

    def test_start_date_is_saved_with_job_and_not_reparsed_for_applications(self):
        job = {"company":"ABC", "title":"Co-op - Starting Winter 2027", "location":"Ottawa", "url":"https://example.com/start", "description":"Details", "description_hash":"start", "start_date":"Winter 2027"}
        job_id, _ = self.db.upsert_job(job)
        self.assertEqual(self.db.job(job_id)["start_date"], "Winter 2027")

    def test_localization_reads_settings_once_per_language_pass(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from unittest.mock import patch
        from PySide6.QtWidgets import QApplication, QLabel, QWidget
        from app.i18n import localize_widget_tree
        app = QApplication.instance() or QApplication([])
        root = QWidget(); label = QLabel("Dashboard", root)
        with patch("app.i18n.load_settings", return_value={"language": "zh"}) as load:
            localize_widget_tree(root)
        self.assertEqual(load.call_count, 1)
        self.assertEqual(label.text(), "仪表盘")

    def test_job_filters_use_untranslated_internal_values(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from app.gui import JobsPage
        app = QApplication.instance() or QApplication([])
        self.db.upsert_job({"company":"ABC", "title":"Engineer", "location":"Ottawa", "url":"https://example.com/filter", "description":"", "description_hash":"filter"})
        page = JobsPage(self.db, type("J", (), {})(), type("R", (), {})(), type("A", (), {"available_models": lambda self: []})())
        page.status_filter.setItemText(0, "所有状态")
        page.location_filter.setItemText(0, "所有地点")
        self.assertEqual(len(page.filtered()), 1)

    def test_docx_resume_import_extracts_text_and_keeps_source(self):
        from docx import Document
        source = Path(self.tmp.name) / "resume.docx"
        document = Document(); document.add_paragraph("Mechanical engineering candidate"); document.save(source)
        service = ResumeService(self.db, DummyAI()); service.paths = type("P", (), {"resumes_original": Path(self.tmp.name) / "resumes"})()
        service.paths.resumes_original.mkdir()
        extracted = service.import_original(str(source))
        self.assertIn("Mechanical engineering candidate", extracted.read_text(encoding="utf-8"))
        self.assertTrue(list(service.paths.resumes_original.glob("source-*.docx")))

    def test_docx_export_uses_professional_layout(self):
        root = Path(self.tmp.name)
        professional, layout = render_resume_docx("Jane Doe\n\nPROJECTS\n- Built a test rig.", root / "professional.docx", {"docx_layout": "Professional one-page"})
        self.assertTrue(professional.exists())
        self.assertEqual(layout, "Professional one-page")

    def test_data_directory_migration_copies_database_and_keeps_source(self):
        original_locator = config.LOCATION_FILE
        try:
            root = Path(self.tmp.name); source = root / "source"; destination = root / "destination"
            locator = root / "locator.json"; config.LOCATION_FILE = locator
            locator.write_text(json.dumps({"data_dir": str(source)}), encoding="utf-8")
            paths = config.ensure_directories({**config.DEFAULT_SETTINGS, "data_dir": str(source)})
            connection = sqlite3.connect(paths.database)
            try:
                connection.execute("CREATE TABLE sample(value TEXT)"); connection.execute("INSERT INTO sample VALUES('kept')"); connection.commit()
            finally:
                connection.close()
            settings = config.load_settings(); old, new = config.migrate_data_directory(settings, str(destination))
            self.assertTrue((old / "data" / "careeros.db").exists())
            connection = sqlite3.connect(new / "data" / "careeros.db")
            try:
                self.assertEqual(connection.execute("SELECT value FROM sample").fetchone()[0], "kept")
            finally:
                connection.close()
        finally:
            config.LOCATION_FILE = original_locator

    def test_database_merge_remaps_jobs_and_copies_only_source_tree_files(self):
        root = Path(self.tmp.name); source_root = root / "source-data"; target_root = root / "target-data"
        source_db_path = source_root / "data" / "careeros.db"; source_db = Database(source_db_path)
        job_id, _ = source_db.upsert_job({"company":"Merged Co", "title":"Engineer", "location":"Ottawa", "url":"https://example.com/merged", "description":"CAD", "description_hash":"merged"})
        original = source_root / "resumes" / "original" / "original-merge.txt"; original.parent.mkdir(parents=True); original.write_text("Jane Doe\nSKILLS\nCAD", encoding="utf-8")
        version_id = source_db.add_resume_version(job_id=job_id, version_name="resume_v001", content="draft", source_path=str(original), document_type="Resume")
        source_db.set_resume_decision(version_id, True); source_db.mark_applied(job_id)
        paths = type("P", (), {"backups":target_root / "backups", "resumes_original":target_root / "resumes" / "original", "resumes_generated":target_root / "resumes" / "generated", "resumes_approved":target_root / "resumes" / "approved", "cover_letters":target_root / "cover_letters", "supporting_documents":target_root / "supporting_documents"})()
        for value in vars(paths.__class__).values():
            if isinstance(value, Path): value.mkdir(parents=True, exist_ok=True)
        target = Database(target_root / "data" / "careeros.db")
        with patch("app.database.get_paths", return_value=paths):
            result = target.merge_from(source_db_path)
        self.assertEqual(result["jobs"], 1); self.assertEqual(result["drafts"], 1); self.assertEqual(result["applications"], 1)
        merged = target.resume_versions()[0]
        self.assertTrue(Path(merged["source_path"]).is_file())
        self.assertEqual(target.job(merged["job_id"])["company"], "Merged Co")

    def test_supporting_document_delete_refuses_path_outside_storage(self):
        root = Path(self.tmp.name); storage = root / "supporting"; storage.mkdir(); outside = root / "keep.txt"; outside.write_text("keep", encoding="utf-8")
        service = SupportingDocumentService(self.db); service.paths = type("P", (), {"supporting_documents": storage})()
        document_id = self.db.add_supporting_document("tampered.txt", str(outside), None, "ready", 4)
        service.remove(document_id)
        self.assertTrue(outside.exists())

    def test_legacy_applypilot_database_is_copied_to_careeros_database(self):
        root = Path(self.tmp.name) / "legacy-root"
        legacy = root / "data" / config.LEGACY_DATABASE_FILENAME
        legacy.parent.mkdir(parents=True)
        connection = sqlite3.connect(legacy)
        try:
            connection.execute("CREATE TABLE retained(value TEXT)")
            connection.execute("INSERT INTO retained VALUES('safe')")
            connection.commit()
        finally:
            connection.close()
        paths = config.ensure_directories({**config.DEFAULT_SETTINGS, "data_dir": str(root)})
        self.assertEqual(paths.database.name, "careeros.db")
        self.assertTrue(legacy.exists())
        connection = sqlite3.connect(paths.database)
        try:
            self.assertEqual(connection.execute("SELECT value FROM retained").fetchone()[0], "safe")
        finally:
            connection.close()

    def test_supporting_text_file_is_imported_for_ai(self):
        source = Path(self.tmp.name) / "project-notes.txt"; source.write_text("Validated CAD and MATLAB project work", encoding="utf-8")
        service = SupportingDocumentService(self.db); service.paths = type("P", (), {"supporting_documents": Path(self.tmp.name) / "materials"})()
        service.paths.supporting_documents.mkdir()
        result = service.import_file(str(source))
        self.assertEqual(result["status"], "ready")
        self.assertGreater(self.db.supporting_documents()[0]["character_count"], 0)

    def test_pdf_outputs_are_created_and_readable(self):
        from pypdf import PdfReader
        resume_pdf = Path(self.tmp.name) / "resume.pdf"; cover_pdf = Path(self.tmp.name) / "cover.pdf"
        render_resume_pdf("Jane Doe\nMechanical Engineer\n\nEXPERIENCE\n- Designed CAD fixtures", resume_pdf)
        render_cover_letter_pdf("Dear Hiring Manager,\n\nI am interested in this role.\n\nSincerely,\nJane", cover_pdf, "Example Corp", "Mechanical Engineer")
        self.assertGreater(len(PdfReader(str(resume_pdf)).pages), 0)
        self.assertIn("Cover Letter", "".join(page.extract_text() or "" for page in PdfReader(str(cover_pdf)).pages))

    def test_resume_pdf_has_no_careeros_footer_and_can_span_pages(self):
        from pypdf import PdfReader
        resume_pdf = Path(self.tmp.name) / "long-resume.pdf"
        content = "Jane Doe\n\nEXPERIENCE\n" + "\n".join(f"- Designed and validated a detailed engineering component for project {index}." for index in range(180))
        render_resume_pdf(content, resume_pdf, {"style": "Compact", "font_size": 8, "margins": "Narrow"})
        reader = PdfReader(str(resume_pdf))
        self.assertGreater(len(reader.pages), 1)
        self.assertNotIn("CareerOS draft", "".join(page.extract_text() or "" for page in reader.pages))


if __name__ == "__main__": unittest.main()
