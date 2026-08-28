from __future__ import annotations

import difflib
import hashlib
import html
import json
import logging
import re
import sys
import time
import traceback
import unicodedata
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QElapsedTimer, QEvent, QEventLoop, QObject, QPropertyAnimation, QRect, QSize, QThread, QTimer, Signal, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QLinearGradient, QPainter, QPixmap
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QAbstractItemView, QAbstractSpinBox, QGraphicsOpacityEffect, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget, QSizePolicy, QSplashScreen,
)

from app.ai_manager import AIManager
from app.database import Database
from app.form_fill import CONTACT_FIELDS, build_form_fill_script, form_fill_values
from app.i18n import localize_widget_tree, translate
from app.secrets import protect_secret
from app.services import CoverLetterService, JobService, ResumeService, SupportingDocumentService, compact_job_text, evaluate_requirement, extract_job_facts
from config import get_paths, load_settings, migrate_data_directory, save_settings


STATUSES = ["New", "Interested", "Review", "Preparing", "Ready", "Applied", "Interview", "Rejected", "Offer", "Ignored"]
MAX_BATCH_JOBS = 20
SEARCH_SITES = {
    "indeed": "Indeed",
    "linkedin": "LinkedIn",
    "google": "Google Jobs",
    "glassdoor": "Glassdoor",
    "zip_recruiter": "ZipRecruiter",
}
logger = logging.getLogger(__name__)


class WheelFocusGuard(QObject):
    """Allow wheel changes only after an explicit mouse click on that control."""
    @staticmethod
    def _control(watched):
        control = watched
        while control is not None and not isinstance(control, (QComboBox, QSpinBox, QSlider)):
            control = control.parentWidget()
        return control

    def eventFilter(self, watched, event):
        control = self._control(watched)
        if control is None:
            return super().eventFilter(watched, event)
        if event.type() == QEvent.MouseButtonPress:
            control.setProperty("_careeros_wheel_armed", True)
        elif event.type() == QEvent.FocusOut:
            control.setProperty("_careeros_wheel_armed", False)
        elif event.type() == QEvent.Wheel:
            focused = QApplication.focusWidget()
            line_edit = control.lineEdit() if isinstance(control, QSpinBox) else None
            if not control.property("_careeros_wheel_armed") or focused not in (control, line_edit):
                event.ignore()
                return True
        return super().eventFilter(watched, event)


class TaskThread(QThread):
    completed = Signal(object)
    failed = Signal(str)
    progress = Signal(str)

    def __init__(self, func, *args, with_progress=False):
        super().__init__()
        self.func, self.args, self.with_progress = func, args, with_progress

    def run(self):
        try:
            result = self.func(*self.args, self.progress.emit) if self.with_progress else self.func(*self.args)
            self.completed.emit(result)
        except Exception as exc:
            logger.exception("Background task failed")
            self.failed.emit(f"{exc}\n\n{traceback.format_exc(limit=3)}")


class DashboardPage(QWidget):
    def __init__(self, db: Database):
        super().__init__(); self.db = db; layout = QVBoxLayout(self); layout.setContentsMargins(22, 20, 22, 22); layout.setSpacing(14)
        title = QLabel("<h1>Dashboard</h1><p style='color:#6e6e73;margin-top:-6px'>Your job search at a glance.</p>"); title.setObjectName("pageTitle"); layout.addWidget(title)
        metrics = QHBoxLayout(); metrics.setSpacing(10); self.metric_values = {}
        for key, caption in (("found", "Jobs found"), ("good", "Good matches"), ("ready", "Ready"), ("applied", "Applied"), ("interviews", "Interviews")):
            card = QWidget(); card.setObjectName("metricCard"); card_layout = QVBoxLayout(card); card_layout.setContentsMargins(15, 12, 15, 12); card_layout.setSpacing(2); value = QLabel("0"); value.setObjectName("metricValue"); label = QLabel(caption); label.setObjectName("metricCaption"); card_layout.addWidget(value); card_layout.addWidget(label); metrics.addWidget(card, 1); self.metric_values[key] = value
        layout.addLayout(metrics)
        recent_panel = QWidget(); recent_panel.setObjectName("dashboardPanel"); panel_layout = QVBoxLayout(recent_panel); panel_layout.setContentsMargins(16, 14, 16, 12); panel_layout.setSpacing(7); panel_layout.addWidget(QLabel("<b>Recent jobs</b><br/><span style='color:#6e6e73;font-size:11px'>Latest positions saved in CareerOS</span>"))
        self.recent = QListWidget(); self.recent.setObjectName("recentList"); self.recent.setSelectionMode(QAbstractItemView.NoSelection); self.recent.setFocusPolicy(Qt.NoFocus); panel_layout.addWidget(self.recent); layout.addWidget(recent_panel, 1)
        self.refresh()

    def refresh(self):
        jobs = self.db.jobs(); count = lambda s: sum(j["status"] == s for j in jobs)
        good = sum((j["match_score"] or 0) >= 75 for j in jobs)
        values = {"found": len(jobs), "good": good, "ready": count("Ready"), "applied": count("Applied"), "interviews": count("Interview")}
        for key, value in values.items(): self.metric_values[key].setText(str(value))
        self.recent.clear()
        if not jobs:
            empty = QListWidgetItem("No jobs yet. Use Search Jobs or Add Job to begin."); empty.setFlags(Qt.NoItemFlags); self.recent.addItem(empty); return
        for job in jobs[:12]:
            item = QListWidgetItem(); item.setSizeHint(QSize(0, 54)); row = QWidget(); row.setObjectName("recentRow"); row_layout = QVBoxLayout(row); row_layout.setContentsMargins(8, 4, 8, 4); row_layout.setSpacing(1)
            company = html.escape(job.get("company") or "Unknown company"); title = html.escape(job.get("title") or "Untitled role"); location = html.escape(job.get("location") or "Location not listed")
            row_layout.addWidget(QLabel(f"<b>{company}</b> <span style='color:#6e6e73'>- {title}</span>")); row_layout.addWidget(QLabel(f"<span style='color:#6e6e73;font-size:11px'>{location}</span>")); self.recent.addItem(item); self.recent.setItemWidget(item, row)


class JobDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent); self.setWindowTitle("Add Job"); self.resize(620, 520)
        self.company, self.title, self.location, self.url = QLineEdit(), QLineEdit(), QLineEdit(), QLineEdit()
        self.description = QTextEdit(); form = QFormLayout(self)
        for label, widget in (("Company", self.company), ("Position", self.title), ("Location", self.location), ("URL", self.url), ("Description", self.description)): form.addRow(label, widget)
        save = QPushButton("Add Job"); save.clicked.connect(self.accept); form.addRow(save)

    def values(self):
        return {"company": self.company.text(), "title": self.title.text(), "location": self.location.text(), "url": self.url.text(), "source": "manual", "description": self.description.toPlainText()}


class JobsPage(QWidget):
    changed = Signal()
    resume_ready = Signal()

    def __init__(self, db: Database, jobs: JobService, resumes: ResumeService, covers: CoverLetterService, ai: AIManager):
        super().__init__(); self.db, self.service, self.resumes, self.covers, self.ai = db, jobs, resumes, covers, ai; self.current_job = None; self.worker = None; self.task_started_at = None
        root = QVBoxLayout(self); root.setContentsMargins(22, 20, 22, 22); root.setSpacing(10); title_label = QLabel("<h1>Jobs</h1><p style='color:#6e6e73;margin-top:-7px'>Search, review and organize opportunities.</p>"); title_label.setMinimumHeight(74); root.addWidget(title_label)
        controls = QHBoxLayout(); self.search_box = QLineEdit(); self.search_box.setPlaceholderText("Search company, position, location"); self.search_box.textChanged.connect(self.refresh)
        self.score_filter = QComboBox(); self.score_filter.addItem("All matches", ""); self.score_filter.addItem("75+ Good", "75"); self.score_filter.addItem("60+ Possible", "60"); self.score_filter.addItem("Unscored", "unscored"); self.score_filter.currentTextChanged.connect(self.refresh)
        self.status_filter = QComboBox(); self.status_filter.addItem("All statuses", ""); [self.status_filter.addItem(status, status) for status in STATUSES]; self.status_filter.currentTextChanged.connect(self.refresh)
        self.location_filter = QComboBox(); self.location_filter.addItem("All locations", ""); [self.location_filter.addItem(location, location) for location in load_settings().get("search", {}).get("locations", [])]; self.location_filter.currentTextChanged.connect(self.refresh)
        self.source_filter = QComboBox(); self.source_filter.addItem("All sources", "")
        for key, label in SEARCH_SITES.items(): self.source_filter.addItem(label, key)
        self.source_filter.addItem("Manual", "manual"); self.source_filter.addItem("Import", "import"); self.source_filter.currentTextChanged.connect(self.refresh)
        self.model = QComboBox(); self.refresh_models()
        search_btn = QPushButton("Search Jobs"); search_btn.clicked.connect(self.run_search); add_btn = QPushButton("Add Job"); add_btn.clicked.connect(self.add_job); import_btn = QPushButton("Import"); import_btn.clicked.connect(self.import_jobs)
        for w in (self.search_box, self.score_filter, self.status_filter, self.location_filter, self.source_filter, self.model, search_btn, add_btn, import_btn): controls.addWidget(w)
        root.addLayout(controls)
        selection_hint = QLabel("Tip: use Ctrl or Shift to select multiple jobs (maximum 20 per batch)."); root.addWidget(selection_hint)
        split = QSplitter(); self.table = QTableWidget(0, 7); self.table.setHorizontalHeaderLabels(["Company", "Match", "Position", "Location", "Source", "Salary", "Status"]); self.table.horizontalHeader().setMinimumSectionSize(75)
        for column in (1, 4, 5, 6): self.table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignCenter)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setSelectionMode(QAbstractItemView.ExtendedSelection); self.table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.table.setSortingEnabled(True); self.table.itemSelectionChanged.connect(self.show_selected)
        detail = QWidget(); dl = QVBoxLayout(detail); self.detail_title = QLabel("Select a job"); self.detail_title.setWordWrap(True); self.detail = QTextEdit(); self.detail.setReadOnly(True); self.description = QTextEdit(); self.description.setReadOnly(True); self.detail_tabs = QTabWidget(); self.detail_tabs.addTab(self.detail, "Requirements && Match"); self.detail_tabs.addTab(self.description, "Full Description"); self.status = QComboBox(); self.status.addItems(STATUSES); self.status.currentTextChanged.connect(self.change_status)
        actions = QHBoxLayout(); analyze = QPushButton("Analyze Selected"); analyze.clicked.connect(self.analyze); optimize = QPushButton("Resume Drafts"); optimize.clicked.connect(self.optimize); cover = QPushButton("Cover Drafts"); cover.clicked.connect(self.cover_letter); open_btn = QPushButton("Open Job Page"); open_btn.clicked.connect(self.open_job); fill_btn = QPushButton("Copy Form Fill Script"); fill_btn.clicked.connect(self.copy_form_fill_script)
        for w in (analyze, optimize, cover, open_btn, fill_btn): actions.addWidget(w)
        translation_controls = QHBoxLayout(); self.translate_button = QPushButton("Translate 中文"); self.translate_button.clicked.connect(self.translate_description); self.show_chinese = QCheckBox("Show Chinese"); self.show_chinese.setEnabled(False); self.show_chinese.stateChanged.connect(self.toggle_translation); self.translation_status = QLabel("Original text"); translation_controls.addWidget(self.translate_button); translation_controls.addWidget(self.show_chinese); translation_controls.addWidget(self.translation_status); translation_controls.addStretch()
        dl.addWidget(self.detail_title); dl.addWidget(self.status); dl.addLayout(actions); dl.addLayout(translation_controls); dl.addWidget(self.detail_tabs)
        split.addWidget(self.table); split.addWidget(detail); split.setSizes([650, 650]); root.addWidget(split); root.setStretch(3, 1)
        self.progress = QLabel("AI: Ready" if ai.available_models() else "AI: Offline")
        self.task_progress = QProgressBar(); self.task_progress.setTextVisible(False); self.task_progress.setMaximumHeight(8); self.task_progress.hide()
        self.task_timing = QLabel(""); self.task_timing.setStyleSheet("color:#6e6e73")
        root.addWidget(self.task_progress); root.addWidget(self.progress); root.addWidget(self.task_timing); self.refresh()

    def refresh_models(self):
        selected = self.model.currentText() if self.model.count() else load_settings().get("ai_mode", "Auto")
        settings = load_settings(); values = ["Auto", settings["models"]["deep"], settings["models"]["fast"]]
        api = settings.get("api", {})
        if api.get("enabled") and api.get("model") and api.get("encrypted_key"):
            values.append(f"API: {api['model']}")
        self.model.clear(); self.model.addItems(list(dict.fromkeys(values)))
        self.model.setCurrentText(selected if selected in values else settings.get("ai_mode", "Auto"))

    def refresh_location_choices(self):
        selected = self.location_filter.currentData(); values = load_settings().get("search", {}).get("locations", [])
        self.location_filter.blockSignals(True); self.location_filter.clear(); self.location_filter.addItem("All locations", "")
        for location in values: self.location_filter.addItem(location, location)
        index = self.location_filter.findData(selected); self.location_filter.setCurrentIndex(index if index >= 0 else 0); self.location_filter.blockSignals(False)

    def confirm_external_ai(self, action: str, jobs: list[dict]) -> bool:
        if not self.model.currentText().startswith("API:"):
            return True
        api = load_settings().get("api", {})
        provider = api.get("base_url") or "configured API provider"
        message = (
            f"This will send resume/profile facts and {len(jobs)} job description(s) to:\n{provider}\n\n"
            f"Action: {action}\nNo application will be submitted. Continue?"
        )
        return QMessageBox.question(self, "Send data to external AI?", message) == QMessageBox.Yes

    def filtered(self):
        normalize = lambda value: "".join(ch for ch in unicodedata.normalize("NFKD", str(value).casefold()) if not unicodedata.combining(ch))
        jobs = self.db.jobs(); q = normalize(self.search_box.text()); score = self.score_filter.currentData(); status = self.status_filter.currentData(); location = normalize(self.location_filter.currentData() or ""); source = self.source_filter.currentData()
        out = []
        for job in jobs:
            if q and q not in normalize(f"{job['company']} {job['title']} {job['location']}"): continue
            value = job["match_score"] if job["match_score"] is not None else job["rule_score"]
            if score == "75" and (value is None or value < 75): continue
            if score == "60" and (value is None or value < 60): continue
            if score == "unscored" and value is not None: continue
            if status and job["status"] != status: continue
            if location and location not in normalize(job["location"]): continue
            if source and source.casefold() != job["source"].casefold(): continue
            out.append(job)
        return out

    def refresh(self):
        selected_id = self.current_job["id"] if self.current_job else None
        rows = self.filtered(); self.table.setSortingEnabled(False); self.table.setRowCount(len(rows)); self.table.setProperty("jobs", rows)
        for r, job in enumerate(rows):
            score = f"{job['match_score']}%" if job["match_score"] is not None else f"~{job['rule_score']}%" if job["rule_score"] is not None else "—"
            for c, value in enumerate((job["company"], score, job["title"], job["location"], job["source"], job["salary"], job["status"])):
                item = QTableWidgetItem(str(value)); item.setData(Qt.UserRole, job["id"])
                if c in (1, 4, 5, 6): item.setTextAlignment(Qt.AlignCenter)
                if c == 1 and job["match_score"] is not None:
                    item.setForeground(QColor("#15803d")); font = item.font(); font.setBold(True); item.setFont(font)
                self.table.setItem(r, c, item)
            if job["id"] == selected_id:
                self.table.selectRow(r)
        self.table.resizeColumnsToContents(); self.table.setColumnWidth(0, 150); self.table.setColumnWidth(1, 30); self.table.setColumnWidth(2, 450); self.table.setColumnWidth(3, 200); self.table.setColumnWidth(4, 30); self.table.setColumnWidth(5, 60); self.table.setColumnWidth(6, 30)
        self.table.setSortingEnabled(True)
        if not rows and not self.current_job:
            self.detail.setPlainText("No jobs yet.\nAdd, import, or search for a job to get started.")
            self.description.clear()

    def show_selected(self):
        row = self.table.currentRow()
        if row < 0: return
        job_id = self.table.item(row, 0).data(Qt.UserRole); self.current_job = self.db.job(job_id); j = self.current_job
        self.detail_title.setText(f"<h3>{j['title']}</h3><b>{j['company']}</b> · {j['location']}")
        self.status.blockSignals(True); self.status.setCurrentText(j["status"]); self.status.blockSignals(False)
        source_hash = hashlib.sha256(compact_job_text(j["description"]).encode("utf-8", "ignore")).hexdigest()
        translation_ready = bool(j.get("translation_zh") and j.get("translation_hash") == source_hash)
        self.show_chinese.blockSignals(True); self.show_chinese.setEnabled(translation_ready)
        if not translation_ready: self.show_chinese.setChecked(False)
        self.show_chinese.blockSignals(False)
        strengths = "\n".join("✓ " + x for x in json.loads(j["strengths_json"] or "[]")); missing = "\n".join("△ " + x for x in json.loads(j["missing_json"] or "[]")); risks = "\n".join("- " + x for x in json.loads(j["risks_json"] or "[]"))
        facts = extract_job_facts(j["title"], j["description"])
        try:
            resume_facts = self.resumes.candidate_context()
        except Exception:
            resume_facts = ""
        profile = load_settings().get("profile", {})
        candidate_facts = resume_facts + "\n" + "\n".join(str(value) for value in profile.values())
        status_label = {"LIKELY_MET": "✓ Likely met", "NOT_CONFIRMED": "? Not confirmed", "DOES_NOT_MEET": "✗ Does not meet"}
        def requirement_lines(values):
            return "\n".join(f"{status_label[evaluate_requirement(value, candidate_facts)]} — {value}" for value in values) or "Not stated"
        education = requirement_lines(facts["education"])
        other_requirements = requirement_lines(facts["other_requirements"])
        match = f"{j['match_score']}% (final)" if j["match_score"] is not None else f"~{j['rule_score']}% (preliminary rules only)" if j["rule_score"] is not None else "Not analyzed"
        self.detail.setPlainText(f"Match: {match}\nRecommendation: {j['recommendation'] or 'Run Analyze for final recommendation'}\nModel: {j['model_used'] or '—'}\nSource: {j['source'] or '—'}\nPosted: {j['date_posted'] or 'Not stated'}\nStart date: {facts['start_date']}\nSalary: {j['salary'] or '—'}\nURL: {j['url'] or '—'}\n\nEducation requirement\n{education}\n\nOther requirements\n{other_requirements}\n\nStrengths\n{strengths or 'Run Analyze to generate'}\n\nMissing\n{missing or 'Run Analyze to generate'}\n\nWarnings\n{risks or 'Run Analyze to generate'}\n\nReason\n{j['match_reason'] or 'Run Analyze to generate'}")
        self.description.setPlainText(compact_job_text(j["translation_zh"] if translation_ready and self.show_chinese.isChecked() else j["description"]) or "No description available.")
        self.translation_status.setText(f"Chinese: {j.get('translation_model') or 'Ready'}" if translation_ready else "Original text")

    @staticmethod
    def _duration(seconds: float) -> str:
        seconds = max(0, round(seconds)); return f"{seconds // 60}:{seconds % 60:02d}"

    def busy(self, text):
        self.progress.setText(translate(text))
        match = re.search(r"(?:Searching|Analyzing|Resume|Cover letter) (\d+)/(\d+):", text)
        if not match or not self.task_started_at:
            return
        index, total = map(int, match.groups()); completed = max(0, index - 1); elapsed = time.monotonic() - self.task_started_at
        self.task_progress.setRange(0, total); self.task_progress.setValue(completed); self.task_progress.show()
        if completed:
            remaining = elapsed / completed * (total - completed)
            self.task_timing.setText(translate(f"Progress {completed}/{total} · elapsed {self._duration(elapsed)} · about {self._duration(remaining)} remaining"))
        else:
            self.task_timing.setText(translate(f"Progress 0/{total} · estimating time remaining..."))

    def finish_task_progress(self):
        if self.task_progress.isVisible():
            self.task_progress.setRange(0, 100); self.task_progress.setValue(100)
            elapsed = time.monotonic() - self.task_started_at if self.task_started_at else 0
            self.task_timing.setText(translate(f"Completed in {self._duration(elapsed)}"))
        self.task_started_at = None

    def fail(self, text):
        self.finish_task_progress(); self.progress.setText(translate("Error")); QMessageBox.critical(self, "CareerOS", text)
    def run_task(self, func, done, *args, with_progress=False):
        if self.worker and self.worker.isRunning():
            self.busy("Another task is still running")
            QMessageBox.information(self, "CareerOS", "Another search or AI task is still running.")
            return False
        self.task_started_at = time.monotonic(); self.task_progress.setRange(0, 0); self.task_progress.show(); self.task_timing.setText(translate("Starting task..."))
        self.worker = TaskThread(func, *args, with_progress=with_progress)
        def completed(result):
            self.finish_task_progress(); done(result)
        self.worker.completed.connect(completed); self.worker.failed.connect(self.fail); self.worker.progress.connect(self.busy); self.worker.start()
        return True

    def selected_jobs(self) -> list[dict]:
        indexes = self.table.selectionModel().selectedRows(0)
        job_ids = list(dict.fromkeys(self.table.item(index.row(), 0).data(Qt.UserRole) for index in indexes))
        if not job_ids and self.current_job:
            job_ids = [self.current_job["id"]]
        return [job for job_id in job_ids if (job := self.db.job(job_id))]

    def confirmed_batch(self, action: str) -> list[dict]:
        jobs = self.selected_jobs()
        if not jobs:
            QMessageBox.information(self, "CareerOS", "Select at least one job first.")
            return []
        if len(jobs) > MAX_BATCH_JOBS:
            QMessageBox.warning(self, "Batch limit", f"Select no more than {MAX_BATCH_JOBS} jobs per batch. You selected {len(jobs)}.")
            return []
        if len(jobs) > 1:
            note = "This creates local drafts only; it never submits applications." if action != "analyze" else "Results are saved locally; no applications are submitted."
            question = f"Run {action} for {len(jobs)} selected jobs?\n\n{note}\nThis may take a long time with local AI."
            if QMessageBox.question(self, "Confirm batch", question) != QMessageBox.Yes:
                return []
        return jobs

    def _analyze_many(self, jobs, resume, model, progress):
        completed, errors, models = 0, [], []
        for index, job in enumerate(jobs, 1):
            progress(f"Analyzing {index}/{len(jobs)}: {job['company']} — {job['title']}")
            try:
                result = self.service.analyze(job["id"], resume, model); completed += 1; models.append(result["model_used"])
            except Exception as exc:
                errors.append(f"{job['company']} — {job['title']}: {exc}")
        return {"completed": completed, "errors": errors, "models": list(dict.fromkeys(models))}

    def _optimize_many(self, jobs, model, progress):
        completed, errors, versions = 0, [], []
        for index, job in enumerate(jobs, 1):
            progress(f"Resume {index}/{len(jobs)}: {job['company']} — {job['title']}")
            try:
                result = self.resumes.optimize(job, model); completed += 1; versions.append(result["version_name"])
            except Exception as exc:
                errors.append(f"{job['company']} — {job['title']}: {exc}")
        return {"completed": completed, "errors": errors, "versions": versions}

    def _cover_many(self, jobs, model, progress):
        completed, errors, paths = 0, [], []
        for index, job in enumerate(jobs, 1):
            progress(f"Cover letter {index}/{len(jobs)}: {job['company']} — {job['title']}")
            try:
                result = self.covers.generate(job, model); path = self.covers.save(job, result["letter"], result["model_used"]); completed += 1; paths.append(str(path))
            except Exception as exc:
                errors.append(f"{job['company']} — {job['title']}: {exc}")
        return {"completed": completed, "errors": errors, "paths": paths}

    def show_batch_result(self, title: str, result: dict):
        errors = result.get("errors", [])
        message = f"Completed: {result.get('completed', 0)}\nErrors: {len(errors)}"
        if errors:
            message += "\n\n" + "\n".join(errors[:8])
        QMessageBox.information(self, title, message)

    def run_search(self):
        self.busy("Searching...")
        self.run_task(self.service.search, lambda r: (self.busy(f"Search complete: {r['added']} new, {r['existing']} existing, {r['errors']} warnings"), self.refresh(), self.changed.emit()), with_progress=True)

    def add_job(self):
        dialog = JobDialog(self)
        if dialog.exec() and dialog.values()["title"]: self.service.add_manual(dialog.values()); self.refresh(); self.changed.emit()

    def import_jobs(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Jobs", "", "Job files (*.csv *.json)")
        if path:
            try:
                result = self.service.import_file(path); self.refresh(); self.changed.emit()
                QMessageBox.information(self, "Import Jobs", f"Imported {result['added']} new jobs; {result['existing']} already existed.")
            except Exception as exc:
                self.fail(str(exc))

    def analyze(self):
        jobs = self.confirmed_batch("analyze")
        if not jobs: return
        if not self.confirm_external_ai("Analyze", jobs): return
        try: resume = self.resumes.candidate_context()
        except Exception as exc: return self.fail(str(exc))
        self.busy(f"Analyzing {len(jobs)} selected job(s)..."); model = self.model.currentText()
        def done(result):
            self.busy(f"Analysis complete: {result['completed']} job(s)"); self.refresh(); self.show_selected(); self.changed.emit()
            if len(jobs) > 1 or result["errors"]: self.show_batch_result("Analyze Selected", result)
        self.run_task(self._analyze_many, done, jobs, resume, model, with_progress=True)

    def optimize(self):
        jobs = self.confirmed_batch("resume drafts")
        if not jobs: return
        if not self.confirm_external_ai("Generate resume drafts", jobs): return
        self.busy(f"Generating {len(jobs)} resume draft(s)...")
        def done(result):
            self.busy(f"Created {result['completed']} resume draft(s) for review"); self.changed.emit(); self.resume_ready.emit(); self.show_batch_result("Resume Drafts", result)
        self.run_task(self._optimize_many, done, jobs, self.model.currentText(), with_progress=True)

    def cover_letter(self):
        jobs = self.confirmed_batch("cover letter drafts")
        if not jobs: return
        if not self.confirm_external_ai("Generate cover letter drafts", jobs): return
        if len(jobs) > 1:
            self.busy(f"Generating {len(jobs)} cover letter draft(s)...")
            def batch_done(result):
                self.busy(f"Saved {result['completed']} cover letter draft(s)"); self.changed.emit(); self.show_batch_result("Cover Drafts", result)
            self.run_task(self._cover_many, batch_done, jobs, self.model.currentText(), with_progress=True)
            return
        job = jobs[0]; self.current_job = job
        self.busy("Generating cover letter...")
        def done(result):
            editor = QTextEdit(); editor.setPlainText(result["letter"]); dialog = QDialog(self); dialog.setWindowTitle("Cover Letter Review - PDF Output"); dialog.resize(700, 650); layout = QVBoxLayout(dialog); layout.addWidget(editor); buttons = QHBoxLayout(); preview = QPushButton("Open PDF Preview"); save = QPushButton("Save PDF Draft"); buttons.addWidget(preview); buttons.addWidget(save); layout.addLayout(buttons)
            def show_pdf():
                path = self.covers.preview_pdf(job, editor.toPlainText()); QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            def save_pdf():
                path = self.covers.save(job, editor.toPlainText(), result["model_used"]); QMessageBox.information(dialog, "Cover Letter", f"PDF saved to:\n{path}"); dialog.accept()
            preview.clicked.connect(show_pdf); save.clicked.connect(save_pdf)
            dialog.exec(); self.busy("Ready")
        self.run_task(self.covers.generate, done, job, self.model.currentText())

    def translate_description(self):
        if not self.current_job: return
        job_id = self.current_job["id"]
        self.busy("Translating to Chinese..."); self.translate_button.setEnabled(False)
        def done(result):
            translated_job = self.db.job(job_id); self.translate_button.setEnabled(True)
            if self.current_job and self.current_job["id"] == job_id:
                self.current_job = translated_job; self.show_chinese.setEnabled(True); self.show_chinese.setChecked(True); self.toggle_translation()
            self.busy(f"Translation ready: {result['model_used']}")
        started = self.run_task(self.service.translate, done, job_id, with_progress=True)
        if started:
            self.worker.failed.connect(lambda _: self.translate_button.setEnabled(True))
        else:
            self.translate_button.setEnabled(True)

    def toggle_translation(self):
        if not self.current_job: return
        translated = self.current_job.get("translation_zh") if self.show_chinese.isChecked() else ""
        self.description.setPlainText(compact_job_text(translated or self.current_job["description"]) or "No description available.")
        self.detail_tabs.setCurrentIndex(1)
        self.translation_status.setText(f"Chinese: {self.current_job.get('translation_model') or 'cached'}" if translated else "Original text")

    def open_job(self):
        if self.current_job:
            try: self.service.open_job(self.current_job)
            except Exception as exc: self.fail(str(exc))

    def copy_form_fill_script(self):
        if not self.current_job:
            QMessageBox.information(self, "Form Fill Assistant", "Select a job first.")
            return
        values = form_fill_values(load_settings().get("profile", {}))
        if not values:
            QMessageBox.information(self, "Form Fill Assistant", "Add your contact details in Settings first.")
            return
        fields = ", ".join(CONTACT_FIELDS[key] for key in values)
        message = (
            "CareerOS will copy a temporary script containing these local values:\n"
            f"{fields}\n\n"
            "Open the application page yourself, then paste the script into that page's browser developer console and run it. "
            "It fills only empty text fields. It cannot click, upload files, answer screening questions, or submit anything. "
            "Review every field and submit manually. Continue?"
        )
        if QMessageBox.question(self, "Copy Form Fill Script", message) != QMessageBox.Yes:
            return
        QApplication.clipboard().setText(build_form_fill_script(load_settings().get("profile", {})))
        QMessageBox.information(self, "Form Fill Script Copied", "The script is on your clipboard. Paste and run it only on the application page you are reviewing. CareerOS did not submit an application.")

    def change_status(self, status):
        if self.current_job: self.db.update_job(self.current_job["id"], status=status); self.current_job["status"] = status; self.refresh(); self.changed.emit()


class ResumePage(QWidget):
    def __init__(self, db: Database, resumes: ResumeService, covers: CoverLetterService | None = None):
        super().__init__(); self.db, self.service, self.covers_service, self.current_version, self.current_cover = db, resumes, covers, None, None
        self.original_content = ""
        layout = QVBoxLayout(self); layout.setContentsMargins(22, 20, 22, 22); layout.setSpacing(10); layout.addWidget(QLabel("<h1>Resume & CV</h1><p style='color:#6e6e73;margin-top:-7px'>Review resume drafts and generated cover letters. Red is removed; green is new.</p>")); buttons = QHBoxLayout(); self.preview_button = QPushButton("Open Selected PDF"); self.preview_button.clicked.connect(self.preview_pdf); self.approve_button = QPushButton("Approve Version"); self.approve_button.clicked.connect(lambda: self.decide(True)); self.reject_button = QPushButton("Reject Version"); self.reject_button.clicked.connect(lambda: self.decide(False)); self.regenerate_button = QPushButton("Regenerate"); self.regenerate_button.clicked.connect(self.regenerate); self.delete_button = QPushButton("Delete Selected Draft"); self.delete_button.clicked.connect(self.delete_selected_draft)
        self.preview_zoom = QComboBox(); self.preview_zoom.addItems(["65%", "80%", "100%", "Fit width"]); self.preview_zoom.setCurrentText("80%")
        for w in (self.preview_button, self.approve_button, self.reject_button): w.setMaximumWidth(220); buttons.addWidget(w)
        buttons.addWidget(self.regenerate_button); buttons.addWidget(self.delete_button)
        buttons.addWidget(QLabel("Preview")); buttons.addWidget(self.preview_zoom)
        buttons.addStretch()
        layout.addLayout(buttons)
        prompt_box = QWidget(); prompt_box.setObjectName("settingsCard"); prompt_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum); prompt_box.setMaximumHeight(218); prompt_layout = QFormLayout(prompt_box); prompt_layout.setContentsMargins(16, 12, 16, 12); prompt_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow); prompt_layout.setVerticalSpacing(7); prompt_layout.addRow(QLabel("<b>Generation instructions</b><br/><span style='color:#6e6e73;font-size:11px'>Optional local guidance for the next Resume Draft or Cover Letter. It supplements verification and safety rules.</span>"))
        prompts = load_settings().get("generation_prompts", {}); self.resume_prompt = QTextEdit(); self.resume_prompt.setPlaceholderText("Example: Emphasize CAD and test-fixture work. Keep a concise one-page format."); self.resume_prompt.setPlainText(str(prompts.get("resume", ""))); self.resume_prompt.setFixedHeight(64); self.cover_prompt = QTextEdit(); self.cover_prompt.setPlaceholderText("Example: Use a confident but direct tone and mention interest in aerospace manufacturing."); self.cover_prompt.setPlainText(str(prompts.get("cover_letter", ""))); self.cover_prompt.setFixedHeight(64); self.prompt_status = QLabel("Instructions save automatically after you stop typing."); self.prompt_status.setStyleSheet("color:#6e6e73"); prompt_layout.addRow("Resume Draft", self.resume_prompt); prompt_layout.addRow("Cover Letter", self.cover_prompt); prompt_layout.addRow("Status", self.prompt_status); layout.addWidget(prompt_box)
        self.prompt_timer = QTimer(self); self.prompt_timer.setSingleShot(True); self.prompt_timer.timeout.connect(self.save_generation_prompts); self.resume_prompt.textChanged.connect(self.schedule_prompt_save); self.cover_prompt.textChanged.connect(self.schedule_prompt_save)
        self.versions = QListWidget(); self.versions.setObjectName("versionList"); self.versions.currentRowChanged.connect(self.select_version); self.covers = QListWidget(); self.covers.setObjectName("versionList"); self.covers.currentRowChanged.connect(self.select_cover)
        self.document_lists = QTabWidget(); self.document_lists.setObjectName("documentLists"); self.document_lists.setMaximumHeight(138); self.document_lists.addTab(self.versions, "Resume Drafts"); self.document_lists.addTab(self.covers, "Cover Letters"); layout.addWidget(self.document_lists)
        self.document_stack = QStackedWidget(); self.resume_panel = QWidget(); resume_layout = QVBoxLayout(self.resume_panel); resume_layout.setContentsMargins(0, 0, 0, 0)
        self.resume_tabs = QTabWidget(); self.original_pdf, self.modified_pdf = QPdfDocument(self), QPdfDocument(self); self.original_view, self.modified_view = QPdfView(), QPdfView(); self.original_view.setDocument(self.original_pdf); self.modified_view.setDocument(self.modified_pdf)
        for view in (self.original_view, self.modified_view): view.setPageMode(QPdfView.PageMode.MultiPage)
        comparison = QSplitter(Qt.Horizontal); self.compare_old = self._comparison_text("Original resume", "#8f1d1d"); self.compare_new = self._comparison_text("Generated resume", "#146c3b"); comparison.addWidget(self.compare_old); comparison.addWidget(self.compare_new); comparison.setSizes([600, 600])
        self.resume_tabs.addTab(self.original_view, "Original PDF"); self.resume_tabs.addTab(self.modified_view, "Generated PDF"); self.resume_tabs.addTab(comparison, "Compare Changes")
        resume_layout.addWidget(self.resume_tabs); self.document_stack.addWidget(self.resume_panel)
        self.cover_panel = QWidget(); cover_layout = QVBoxLayout(self.cover_panel); cover_layout.setContentsMargins(0, 0, 0, 0); self.cover_heading = QLabel("<h3>Cover Letter</h3>"); cover_layout.addWidget(self.cover_heading); self.cover_pdf = QPdfDocument(self); self.cover_view = QPdfView(); self.cover_view.setDocument(self.cover_pdf); self.cover_view.setPageMode(QPdfView.PageMode.MultiPage); cover_layout.addWidget(self.cover_view); self.document_stack.addWidget(self.cover_panel)
        self.preview_zoom.currentTextChanged.connect(self.set_preview_zoom); self.set_preview_zoom(self.preview_zoom.currentText()); self.document_lists.currentChanged.connect(self.show_document_category); layout.addWidget(self.document_stack, 1); self.refresh()

    def schedule_prompt_save(self):
        self.prompt_status.setText("Saving instructions..."); self.prompt_timer.start(600)

    def save_generation_prompts(self):
        settings = load_settings(); settings["generation_prompts"] = {"resume": self.resume_prompt.toPlainText().strip(), "cover_letter": self.cover_prompt.toPlainText().strip()}; save_settings(settings); self.prompt_status.setText("Instructions saved locally.")

    def set_preview_zoom(self, value: str):
        views = (self.original_view, self.modified_view, self.cover_view)
        if value == "Fit width":
            for view in views: view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            return
        factor = int(value.rstrip("%")) / 100
        for view in views: view.setZoomMode(QPdfView.ZoomMode.Custom); view.setZoomFactor(factor)

    def show_document_category(self, index: int):
        """Keep the preview in sync when the user switches the list tab itself."""
        if index == 0:
            row = self.versions.currentRow()
            if 0 <= row < len(self.data):
                self.select_version(row)
            else:
                self.current_cover = self.current_version = None; self.approve_button.setEnabled(False); self.reject_button.setEnabled(False); self.document_stack.setCurrentWidget(self.resume_panel)
        else:
            row = self.covers.currentRow()
            if 0 <= row < len(self.cover_data):
                self.select_cover(row)
            else:
                self.current_cover = self.current_version = None; self.approve_button.setEnabled(False); self.reject_button.setEnabled(False); self.document_stack.setCurrentWidget(self.cover_panel)

    @staticmethod
    def _comparison_text(title: str, color: str) -> QTextEdit:
        widget = QTextEdit(); widget.setReadOnly(True); widget.setStyleSheet(f"QTextEdit{{background:#ffffff;padding:14px;border:1px solid #d2d2d7;border-radius:9px;font-family:'Segoe UI';font-size:12px}} h3{{color:{color};margin:0 0 10px 0}} .same{{color:#4b5563}} .changed{{background:{'#fee2e2' if color == '#8f1d1d' else '#dcfce7'};color:{color};padding:2px 3px;border-radius:3px}}")
        widget.setHtml(f"<h3>{title}</h3><p style='color:#6e6e73'>Choose a resume draft to compare.</p>")
        return widget

    def refresh(self):
        try:
            self.original_content = self.service.original_text(); source = self.service.imported_source_path()
            preview = source if source and source.suffix.lower() == ".pdf" else self.service.preview_pdf(self.original_content, "original-preview")
            self._load_pdf(self.original_pdf, preview)
        except Exception:
            self.original_content = ""; self.compare_old.setHtml("<p style='color:#64748b'>Import an original resume in Settings to begin review.</p>")
        self.current_version = self.current_cover = None; self.approve_button.setEnabled(False); self.reject_button.setEnabled(False); self.delete_button.setEnabled(False)
        self.data = self.db.resume_versions(); self.versions.clear()
        for v in self.data: self.versions.addItem(f"{v.get('document_type', 'Resume')} · {v['version_name']} · {v['company'] or ''} · {'Approved' if v['approved'] else 'Rejected' if v['rejected'] else 'Review'}")
        self.cover_data = self.db.cover_letters(); self.covers.clear()
        for cover in self.cover_data:
            date = (cover.get("created_at") or "")[:10]
            self.covers.addItem(f"{cover.get('company') or 'Unknown company'} · {cover.get('title') or 'Cover letter'} · {date}")

    @staticmethod
    def _load_pdf(document: QPdfDocument, path: Path):
        document.close(); document.load(str(path))

    @staticmethod
    def _split_inline_diff(old: str, new: str) -> tuple[str, str]:
        old_tokens, new_tokens = re.findall(r"\s+|\S+", old), re.findall(r"\s+|\S+", new)
        old_parts, new_parts = [], []
        for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, old_tokens, new_tokens).get_opcodes():
            old_part, new_part = html.escape("".join(old_tokens[a0:a1])), html.escape("".join(new_tokens[b0:b1]))
            if tag == "equal": old_parts.append(old_part); new_parts.append(new_part)
            elif tag == "delete": old_parts.append(f"<span style='background:#fee2e2;color:#8f1d1d'>{old_part}</span>")
            elif tag == "insert": new_parts.append(f"<span style='background:#dcfce7;color:#146c3b'>{new_part}</span>")
            else:
                old_parts.append(f"<span style='background:#fee2e2;color:#8f1d1d'>{old_part}</span>")
                new_parts.append(f"<span style='background:#dcfce7;color:#146c3b'>{new_part}</span>")
        return "".join(old_parts), "".join(new_parts)

    @staticmethod
    def _clean_compare_line(line: str) -> str:
        return re.sub(r"^\s*(?:[-*]|[^\w\s])\s+", "- ", line).strip()

    def _compare_html(self, original: str, modified: str) -> str:
        old_rows = ["<h3 style='color:#8f1d1d'>Original resume</h3><p style='color:#6e6e73'>Red material was removed or replaced.</p>"]
        new_rows = ["<h3 style='color:#146c3b'>Generated resume</h3><p style='color:#6e6e73'>Green material is new or replaced.</p>"]
        before = [self._clean_compare_line(line) for line in original.splitlines()]
        after = [self._clean_compare_line(line) for line in modified.splitlines()]
        for tag, a0, a1, b0, b1 in difflib.SequenceMatcher(None, before, after).get_opcodes():
            if tag == "equal":
                old_rows.extend(f"<p style='color:#4b5563'>{html.escape(line)}</p>" for line in before[a0:a1]); new_rows.extend(f"<p style='color:#4b5563'>{html.escape(line)}</p>" for line in after[b0:b1])
            elif tag == "delete":
                old_rows.extend(f"<p style='background:#fee2e2;color:#8f1d1d;padding:3px 4px'>{html.escape(line)}</p>" for line in before[a0:a1])
            elif tag == "insert":
                new_rows.extend(f"<p style='background:#dcfce7;color:#146c3b;padding:3px 4px'>{html.escape(line)}</p>" for line in after[b0:b1])
            else:
                longest = max(a1-a0, b1-b0)
                for index in range(longest):
                    old = before[a0 + index] if a0 + index < a1 else ""; new = after[b0 + index] if b0 + index < b1 else ""
                    old_text, new_text = self._split_inline_diff(old, new)
                    old_rows.append(f"<p style='color:#4b5563;padding:3px 4px'>{old_text}</p>")
                    new_rows.append(f"<p style='color:#4b5563;padding:3px 4px'>{new_text}</p>")
        return "".join(old_rows), "".join(new_rows)

    def select_version(self, row):
        if row < 0 or row >= len(self.data): return
        self.current_cover = None; self.current_version = self.data[row]; content = self.current_version["content"]
        self.approve_button.setEnabled(True); self.reject_button.setEnabled(True); self.delete_button.setEnabled(not bool(self.current_version.get("approved")))
        self._load_pdf(self.modified_pdf, self.service.preview_pdf(content, "modified-preview")); old_html, new_html = self._compare_html(self.original_content, content); self.compare_old.setHtml(old_html); self.compare_new.setHtml(new_html); self.document_stack.setCurrentWidget(self.resume_panel); self.resume_tabs.setCurrentIndex(2)

    def regenerate(self):
        # Read the visible list selection rather than stale preview state.
        # The same button regenerates Resume, CV, or Cover Letter as selected.
        if self.document_lists.currentIndex() == 1:
            row = self.covers.currentRow()
            selected_cover = self.cover_data[row] if 0 <= row < len(self.cover_data) else None
            if not selected_cover:
                QMessageBox.information(self, "Regenerate", "Select a cover letter draft first so CareerOS knows which job to use.")
                return
            if self.covers_service is None:
                QMessageBox.warning(self, "Regenerate", "Cover letter generation is unavailable in this window.")
                return
            job = self.db.job(selected_cover["job_id"])
            if not job:
                QMessageBox.warning(self, "Regenerate", "The original job is no longer available.")
                return
            settings = load_settings()
            if str(settings.get("ai_mode", "Auto")).startswith("API:"):
                if QMessageBox.question(self, "Send data to external AI?", "This will send the resume/profile facts and this job description to your configured API. It creates a local draft only; no application is submitted. Continue?") != QMessageBox.Yes:
                    return
            self.regenerate_button.setEnabled(False); self.regenerate_button.setText("Generating...")
            self.worker = TaskThread(self.covers_service.generate, job)
            def cover_done(result):
                self.covers_service.save(job, result["letter"], result["model_used"])
                self.regenerate_button.setEnabled(True); self.regenerate_button.setText("Regenerate"); self.refresh(); self.document_lists.setCurrentIndex(1); self.covers.setCurrentRow(0)
            def cover_failed(error):
                self.regenerate_button.setEnabled(True); self.regenerate_button.setText("Regenerate"); QMessageBox.critical(self, "Regenerate", error)
            self.worker.completed.connect(cover_done); self.worker.failed.connect(cover_failed); self.worker.start()
            return
        row = self.versions.currentRow()
        selected_version = self.data[row] if 0 <= row < len(self.data) else None
        if not selected_version:
            QMessageBox.information(self, "Regenerate", "Select a Resume or CV draft first so CareerOS knows which job to use.")
            return
        self.current_cover = None; self.current_version = selected_version
        job = self.db.job(selected_version["job_id"])
        if not job:
            QMessageBox.warning(self, "Regenerate", "The original job is no longer available.")
            return
        settings = load_settings()
        if str(settings.get("ai_mode", "Auto")).startswith("API:"):
            if QMessageBox.question(self, "Send data to external AI?", "This will send the resume/profile facts and this job description to your configured API. It creates a local draft only; no application is submitted. Continue?") != QMessageBox.Yes:
                return
        document_type = selected_version.get("document_type", "Resume")
        self.regenerate_button.setEnabled(False); self.regenerate_button.setText("Generating...")
        self.worker = TaskThread(self.service.optimize, job, None, document_type, selected_version)
        def done(_):
            self.regenerate_button.setEnabled(True); self.regenerate_button.setText("Regenerate"); self.refresh(); self.document_lists.setCurrentIndex(0)
            selected_row = next((index for index, version in enumerate(self.data) if version["id"] == selected_version["id"]), 0)
            self.versions.setCurrentRow(selected_row)
        def failed(error):
            self.regenerate_button.setEnabled(True); self.regenerate_button.setText("Regenerate"); QMessageBox.critical(self, "Regenerate", error)
        self.worker.completed.connect(done); self.worker.failed.connect(failed); self.worker.start()

    def delete_selected_draft(self):
        if self.document_lists.currentIndex() == 0:
            row = self.versions.currentRow()
            version = self.data[row] if 0 <= row < len(self.data) else None
            if not version:
                QMessageBox.information(self, "Delete Draft", "Select a draft first.")
                return
            if version.get("approved"):
                QMessageBox.information(self, "Delete Draft", "Approved versions are kept as records and cannot be deleted here.")
                return
            label = version["version_name"]
            if QMessageBox.question(self, "Delete Draft", f"Delete the local {label} draft and its generated PDF? This cannot be undone.") != QMessageBox.Yes:
                return
            record = self.db.remove_resume_version(version["id"])
            if record:
                (self.service.paths.resumes_generated / f"{record['version_name']}.pdf").unlink(missing_ok=True)
        else:
            row = self.covers.currentRow()
            cover = self.cover_data[row] if 0 <= row < len(self.cover_data) else None
            if not cover:
                QMessageBox.information(self, "Delete Draft", "Select a draft first.")
                return
            label = Path(cover["path"]).name
            if QMessageBox.question(self, "Delete Draft", f"Delete the local cover letter {label}? This cannot be undone.") != QMessageBox.Yes:
                return
            record = self.db.remove_cover_letter(cover["id"])
            if record:
                Path(record["path"]).unlink(missing_ok=True)
        self.refresh()

    def select_cover(self, row):
        if row < 0 or row >= len(self.cover_data): return
        self.current_version = None; self.current_cover = self.cover_data[row]
        self.approve_button.setEnabled(False); self.reject_button.setEnabled(False); self.delete_button.setEnabled(True)
        path = Path(self.current_cover["path"])
        self.cover_heading.setText(f"<h3>Cover Letter - {html.escape(self.current_cover.get('company') or '')}</h3>")
        if path.exists(): self._load_pdf(self.cover_pdf, path)
        self.document_stack.setCurrentWidget(self.cover_panel)

    def decide(self, approved):
        if not self.current_version: return
        word = "approve" if approved else "reject"
        if QMessageBox.question(self, "Confirm", f"Do you want to {word} {self.current_version['version_name']}?") == QMessageBox.Yes:
            self.service.decide(self.current_version["id"], approved); self.refresh()

    def preview_pdf(self):
        if self.current_cover:
            path = Path(self.current_cover["path"])
            if path.exists(): QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            return
        content = self.current_version["content"] if self.current_version else self.original_content
        if not content: return
        try:
            name = "selected-resume-preview" if self.current_version else "original-preview"; path = self.service.preview_pdf(content, name); QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            QMessageBox.critical(self, "PDF Preview", str(exc))


class ApplicationsPage(QWidget):
    def __init__(self, db: Database):
        super().__init__(); self.db = db; layout = QVBoxLayout(self); layout.setContentsMargins(22, 20, 22, 22); layout.setSpacing(10); layout.addWidget(QLabel("<h1>Applications</h1><p style='color:#6e6e73;margin-top:-7px'>Tracking only. CareerOS never submits applications.</p>")); self.table = QTableWidget(0, 8); self.table.setHorizontalHeaderLabels(["Company", "Position", "Match", "Status", "Start date", "Post date", "Resume", "Cover Letter"]); self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.table.cellDoubleClicked.connect(lambda *_: self.open_job()); self.table.itemSelectionChanged.connect(self.update_mark_button); layout.addWidget(self.table); actions = QHBoxLayout(); open_job = QPushButton("Open Job Page"); open_job.clicked.connect(self.open_job); self.mark_button = QPushButton("Mark Selected as Applied"); self.mark_button.clicked.connect(self.mark); actions.addWidget(open_job); actions.addWidget(self.mark_button); actions.addStretch(); layout.addLayout(actions); self.refresh()

    def refresh(self):
        jobs = self.db.application_rows(); self.table.setRowCount(len(jobs))
        for r, j in enumerate(jobs):
            start_date = j.get("start_date") or "Not stated"
            post_date = (j["date_posted"] or "")[:10] or "—"
            cover = Path(j["cover_letter_path"]).name if j["cover_letter_path"] else "—"
            for c, value in enumerate((j["company"], j["title"], "—" if j["match_score"] is None else j["match_score"], j["status"], start_date, post_date, j["resume_version"] or "—", cover)):
                item = QTableWidgetItem(str(value)); item.setData(Qt.UserRole, j["id"]); self.table.setItem(r, c, item)
        self.update_mark_button()

    def mark(self):
        row = self.table.currentRow()
        if row < 0:
            return
        job_id = self.table.item(row, 0).data(Qt.UserRole); job = self.db.job(job_id)
        if job and job.get("status") == "Applied":
            if QMessageBox.question(self, "Cancel Applied", "Cancel the local Applied mark and restore the previous status?") == QMessageBox.Yes:
                self.db.unmark_applied(job_id); self.refresh()
        elif QMessageBox.question(self, "Confirm", "Mark this job as manually applied?") == QMessageBox.Yes:
            self.db.mark_applied(job_id); self.refresh()

    def update_mark_button(self):
        row = self.table.currentRow()
        if row < 0:
            self.mark_button.setText("Mark Selected as Applied")
            return
        job = self.db.job(self.table.item(row, 0).data(Qt.UserRole))
        self.mark_button.setText("Unmark as Applied" if job and job.get("status") == "Applied" else "Mark Selected as Applied")

    def open_job(self):
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Open Job Page", "Select an application first.")
            return
        job_id = self.table.item(row, 0).data(Qt.UserRole)
        job = self.db.job(job_id)
        url = str((job or {}).get("url") or "")
        if not url.startswith(("http://", "https://")):
            QMessageBox.warning(self, "Open Job Page", "No valid job link was saved for this application.")
            return
        QDesktopServices.openUrl(QUrl(url))


class SettingsPage(QWidget):
    saved = Signal()
    def __init__(self, resumes: ResumeService, documents: SupportingDocumentService, ai: AIManager):
        super().__init__(); self.resumes, self.documents, self.ai = resumes, documents, ai; self.settings = load_settings()
        root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); scroll = QScrollArea(); scroll.setWidgetResizable(True); body = QWidget(); body_layout = QVBoxLayout(body); body_layout.setContentsMargins(22, 20, 22, 28); body_layout.setSpacing(13); scroll.setWidget(body); root.addWidget(scroll)
        body_layout.addWidget(QLabel("<h1>Settings</h1><p style='color:#6e6e73;margin-top:-6px'>Control where CareerOS stores data and how it uses AI.</p>"))
        def section(title: str, description: str) -> QFormLayout:
            card = QWidget(); card.setObjectName("settingsCard"); card_layout = QVBoxLayout(card); card_layout.setContentsMargins(16, 14, 16, 14); card_layout.setSpacing(9); card_layout.addWidget(QLabel(f"<b>{title}</b><br/><span style='color:#6e6e73;font-size:11px'>{description}</span>")); form = QFormLayout(); form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow); form.setRowWrapPolicy(QFormLayout.WrapLongRows); form.setHorizontalSpacing(18); form.setVerticalSpacing(10); card_layout.addLayout(form); body_layout.addWidget(card); return form
        ai_form = section("AI provider", "AI is optional. Set any locally installed Ollama model names; search jobs works without connecting a model.")
        language_form = section("Language", "Choose the application display language. Existing job data remains unchanged.")
        self.language = QComboBox(); self.language.addItem("English", "en"); self.language.addItem("中文", "zh"); self.language.setCurrentIndex(1 if self.settings.get("language") == "zh" else 0); language_form.addRow("Language", self.language)
        self.model = QComboBox()
        saved_mode = self.settings.get("ai_mode", "Auto"); self._saved_ai_mode = "API" if str(saved_mode).startswith("API:") else str(saved_mode)
        self.detected_local_models = []
        self._populate_local_model_choices([])
        self.local_model_status = QLabel("Detecting installed local models..."); self.local_model_status.setStyleSheet("color:#6e6e73")
        self.refresh_local_models_button = QPushButton("Refresh local models"); self.refresh_local_models_button.clicked.connect(self.detect_local_models)
        local_model_actions = QHBoxLayout(); local_model_actions.addWidget(self.refresh_local_models_button); local_model_actions.addWidget(self.local_model_status); local_model_actions.addStretch()
        self.fallback = QCheckBox("If the selected local model fails, try another detected local model once"); self.fallback.setChecked(self.settings["fallback_enabled"]); ai_form.addRow("Default AI", self.model); ai_form.addRow("", local_model_actions); ai_form.addRow("Local fallback", self.fallback)

        api_form = section("External API", "Optional. Auto stays local; data is sent externally only when you choose API for an action.")
        self.api_card = api_form.parentWidget(); self.api_card.setObjectName("apiSettingsCard")
        api = self.settings.get("api", {})
        self.api_enabled = QCheckBox("Enable an OpenAI-compatible API"); self.api_enabled.setChecked(bool(api.get("enabled")))
        self.api_url = QLineEdit(str(api.get("base_url", "https://api.openai.com/v1"))); self.api_model = QLineEdit(str(api.get("model", "")))
        self.api_key = QLineEdit(); self.api_key.setEchoMode(QLineEdit.Password); self.api_key.setPlaceholderText("Saved securely for this Windows account" if api.get("encrypted_key") else "Paste API key")
        self.clear_key_button = QPushButton("Clear saved API key"); self.clear_key_button.clicked.connect(self.clear_api_key)
        self.api_controls = (self.api_enabled, self.api_url, self.api_model, self.api_key, self.clear_key_button)
        api_form.addRow("API", self.api_enabled); api_form.addRow("Base URL", self.api_url); api_form.addRow("Model", self.api_model); api_form.addRow("API key", self.api_key); api_form.addRow("", self.clear_key_button)
        self.model.currentTextChanged.connect(self.update_api_controls); self.model.currentIndexChanged.connect(self.update_api_controls); self.model.activated.connect(self.update_api_controls)
        self.model.currentTextChanged.connect(lambda _value: QTimer.singleShot(0, self.update_api_controls)); self.update_api_controls(); QTimer.singleShot(0, self.update_api_controls); self.detect_local_models()

        resume_form = section("Resume", "Select the resume CareerOS uses. TXT, PDF and Word are extracted locally; the source is copied and preserved.")
        resume_row = QVBoxLayout(); self.resume_label = QLabel(); self.resume_label.setWordWrap(True); self.resume_label.setObjectName("resumeSelection"); import_resume = QPushButton("Select Resume File..."); import_resume.clicked.connect(self.import_resume); resume_row.addWidget(import_resume); resume_row.addWidget(self.resume_label)
        resume_form.addRow("Active resume", resume_row); self.refresh_resume_label()
        pdf_options = self.settings.get("resume_pdf", {})
        self.resume_pdf_style = QComboBox(); self.resume_pdf_style.addItems(["Modern", "Classic", "Compact"]); self.resume_pdf_style.setCurrentText(str(pdf_options.get("style", "Modern")))
        self.resume_pdf_font_size = QSpinBox(); self.resume_pdf_font_size.setRange(8, 12); self.resume_pdf_font_size.setSuffix(" pt"); self.resume_pdf_font_size.setValue(int(pdf_options.get("font_size", 9)))
        self.resume_pdf_margins = QComboBox(); self.resume_pdf_margins.addItems(["Narrow", "Standard", "Comfortable"]); self.resume_pdf_margins.setCurrentText(str(pdf_options.get("margins", "Standard")))
        resume_form.addRow("PDF style", self.resume_pdf_style); resume_form.addRow("Body font size", self.resume_pdf_font_size); resume_form.addRow("Page margins", self.resume_pdf_margins)

        material_form = section("Additional materials", "Readable PDF, Word, Excel and text files support analysis and draft generation. Other files are stored but not guessed.")
        self.materials = QListWidget(); self.materials.setMinimumHeight(105); materials_row = QVBoxLayout(); materials_row.addWidget(self.materials); material_buttons = QHBoxLayout(); add_material = QPushButton("Add Files..."); add_material.clicked.connect(self.add_materials); remove_material = QPushButton("Remove Selected"); remove_material.clicked.connect(self.remove_material); material_buttons.addWidget(add_material); material_buttons.addWidget(remove_material); material_buttons.addStretch(); materials_row.addLayout(material_buttons)
        material_form.addRow("Extra files", materials_row); self.refresh_materials()

        contact_form = section("Application form details", "Optional local details for the Form Fill Assistant. It only fills fields after you manually run a copied script; it never submits.")
        contact_labels = {key: label for key, label in CONTACT_FIELDS.items()}
        profile_form = section("Verified candidate information", "Enter only facts you personally confirm. These stay local unless you choose API for an AI action.")
        labels = {**contact_labels, "skills":"Skills", "education":"Education", "experience":"Experience", "work_authorization":"Work authorization", "languages":"Languages", "licenses":"Licenses / certifications", "availability":"Availability", "desired_titles":"Desired job titles", "preferred_locations":"Preferred locations", "salary_preference":"Salary preference", "additional_facts":"Additional verified facts"}
        profile = self.settings.get("profile", {})
        self.profile_fields = {}
        for key, label in labels.items():
            field = QLineEdit(str(profile.get(key, ""))); field.setPlaceholderText("User-confirmed facts only")
            self.profile_fields[key] = field; (contact_form if key in contact_labels else profile_form).addRow(label, field)
        search = self.settings.get("search", {})
        search_form = section("Job search", "Search combines your locations and job types. It does not send applications.")
        self.search_locations = QTextEdit(); self.search_locations.setFixedHeight(92); self.search_locations.setPlainText("\n".join(search.get("locations", [])))
        self.search_queries = QTextEdit(); self.search_queries.setFixedHeight(92); self.search_queries.setPlainText("\n".join(search.get("queries", [])))
        self.search_distance = QSpinBox(); self.search_distance.setRange(1, 500); self.search_distance.setSuffix(" miles"); self.search_distance.setValue(int(search.get("distance", 30)))
        self.search_sites = {}
        sites_widget = QWidget(); sites_layout = QHBoxLayout(sites_widget); sites_layout.setContentsMargins(0, 0, 0, 0); sites_layout.setSpacing(10)
        for key, label in SEARCH_SITES.items():
            checkbox = QCheckBox(label); checkbox.setChecked(key in search.get("sites", [])); checkbox.stateChanged.connect(self.update_search_summary); self.search_sites[key] = checkbox; sites_layout.addWidget(checkbox)
        sites_layout.addStretch()
        self.search_summary = QLabel(); self.search_summary.setStyleSheet("color:#6e6e73")
        self.search_locations.textChanged.connect(self.update_search_summary); self.search_queries.textChanged.connect(self.update_search_summary)
        search_form.addRow("Locations", self.search_locations); search_form.addRow("Job types", self.search_queries); search_form.addRow("Sources", sites_widget); search_form.addRow("Distance", self.search_distance); search_form.addRow("Search plan", self.search_summary); self.update_search_summary()
        match_form = section("Match score weighting", "Set how much the final score relies on repeatable resume rules versus AI analysis. The two values always total 100%. Re-run Analyze to apply a new split to existing jobs.")
        weights = self.settings.get("match_weights", {}); rule_value = max(0, min(100, int(weights.get("rule", 70))))
        weight_control = QWidget(); weight_layout = QVBoxLayout(weight_control); weight_layout.setContentsMargins(0, 2, 0, 2); weight_layout.setSpacing(4)
        weight_labels = QHBoxLayout(); self.rule_weight_label = QLabel(); self.ai_weight_label = QLabel(); self.ai_weight_label.setAlignment(Qt.AlignRight); weight_labels.addWidget(self.rule_weight_label); weight_labels.addStretch(); weight_labels.addWidget(self.ai_weight_label)
        self.rule_weight = QSlider(Qt.Horizontal); self.rule_weight.setRange(0, 100); self.rule_weight.setValue(rule_value); self.rule_weight.setTickPosition(QSlider.TickPosition.TicksBelow); self.rule_weight.setTickInterval(10)
        weight_layout.addLayout(weight_labels); weight_layout.addWidget(self.rule_weight)
        self.match_weight_summary = QLabel(); self.match_weight_summary.setStyleSheet("color:#6e6e73")
        self.rule_weight.valueChanged.connect(self.sync_match_weight)
        match_form.addRow("Rule ↔ AI", weight_control); match_form.addRow("Formula", self.match_weight_summary); self.update_match_weight_summary()
        storage_form = section("Storage & safety", "CareerOS keeps applications manual and does not submit forms automatically."); storage_form.addRow("Program directory", QLabel(str(Path(__file__).resolve().parents[1])))
        data_row = QHBoxLayout(); self.data_dir = QLineEdit(str(get_paths().data_dir)); self.data_dir.setReadOnly(True); browse = QPushButton("Browse..."); browse.clicked.connect(self.browse_data_dir); migrate = QPushButton("Copy data here..."); migrate.clicked.connect(self.migrate_data); data_row.addWidget(self.data_dir, 1); data_row.addWidget(browse); data_row.addWidget(migrate); storage_form.addRow("Data directory", data_row); storage_form.addRow("Auto apply", QLabel("Disabled - manual tracking only")); self.autosave_status = QLabel("Settings save automatically after you stop typing."); self.autosave_status.setStyleSheet("color:#6e6e73"); storage_form.addRow("Status", self.autosave_status)
        credit = QLabel("<p style='color:#6e6e73;margin:4px 2px 0'>CareerOS is a local workspace for manual job discovery, review, and application preparation.</p>"); credit.setWordWrap(True); body_layout.addWidget(credit)
        version = QLabel("CareerOS v0.1.5"); version.setObjectName("versionLabel"); version.setAlignment(Qt.AlignCenter); body_layout.addWidget(version); body_layout.addStretch()
        self.autosave_timer = QTimer(self); self.autosave_timer.setSingleShot(True); self.autosave_timer.timeout.connect(self.autosave)
        autosave_widgets = [self.language, self.model, self.fallback, self.api_enabled, self.api_url, self.api_model, self.api_key, self.resume_pdf_style, self.resume_pdf_font_size, self.resume_pdf_margins, *self.profile_fields.values(), self.search_locations, self.search_queries, self.search_distance, *self.search_sites.values()]
        for widget in autosave_widgets:
            signal = getattr(widget, "textChanged", None) or getattr(widget, "currentTextChanged", None) or getattr(widget, "stateChanged", None) or getattr(widget, "valueChanged", None)
            signal.connect(self.schedule_autosave)

    def clear_api_key(self):
        self.settings.setdefault("api", {})["encrypted_key"] = ""; self.api_key.clear(); self.api_key.setPlaceholderText("Paste API key"); self.schedule_autosave()

    @staticmethod
    def _set_combo_choices(combo: QComboBox, values: list[str], selected: str) -> None:
        combo.blockSignals(True); combo.clear(); combo.addItems(list(dict.fromkeys(value for value in values if value))); index = combo.findText(selected)
        combo.setCurrentIndex(index if index >= 0 else 0); combo.blockSignals(False)

    def _populate_local_model_choices(self, detected: list[str]) -> None:
        configured = [str(self.settings["models"].get("deep", "")), str(self.settings["models"].get("fast", ""))]
        self.detected_local_models = list(dict.fromkeys(str(model).strip() for model in detected if str(model).strip()))
        choices = self.detected_local_models or configured
        self._set_combo_choices(self.model, ["Auto", *choices, "API"], self.model.currentText() or self._saved_ai_mode)
        self.update_api_controls()

    def detect_local_models(self):
        if getattr(self, "model_scan", None) and self.model_scan.isRunning():
            return
        self.refresh_local_models_button.setEnabled(False); self.local_model_status.setText("Detecting installed local models...")
        self.model_scan = TaskThread(self.ai.available_models)
        def done(models):
            self._populate_local_model_choices(models)
            self.local_model_status.setText(f"{len(models)} local model(s) detected." if models else "No local models detected; saved choices are shown.")
            self.refresh_local_models_button.setEnabled(True)
        def failed(_):
            self.local_model_status.setText("Could not detect local models."); self.refresh_local_models_button.setEnabled(True)
        self.model_scan.completed.connect(done); self.model_scan.failed.connect(failed); self.model_scan.start()

    def update_api_controls(self, *_):
        """Keep saved API data intact; only the actual API fields are disabled locally."""
        if not hasattr(self, "api_card"):
            return
        api_selected = self.model.currentText().strip().casefold() == "api"
        # Do not disable the card itself: a disabled parent can leave child widgets
        # visually muted after the user switches back to API on some Qt/Windows themes.
        self.api_card.setEnabled(True)
        self.api_card.setProperty("apiFieldsDisabled", not api_selected)
        for control in self.api_controls:
            control.setEnabled(api_selected)
        if api_selected:
            self.api_card.setStyleSheet("QWidget#apiSettingsCard{background:#ffffff;border:1px solid #dde1e8;border-radius:13px} QWidget#apiSettingsCard QLabel,QWidget#apiSettingsCard QCheckBox{color:#1d1d1f;background:transparent} QWidget#apiSettingsCard QLineEdit{background:#fbfcfe;color:#1d1d1f;border:1px solid #d7dbe3;border-radius:9px}")
        else:
            self.api_card.setStyleSheet("QWidget#apiSettingsCard{background:#eceef2;border:1px solid #d7dbe3;border-radius:13px} QWidget#apiSettingsCard QLabel,QWidget#apiSettingsCard QCheckBox{color:#8b93a1;background:transparent} QWidget#apiSettingsCard QLineEdit{background:#e5e7eb;color:#9aa1ad;border:1px solid #d7dbe3;border-radius:9px}")

    def browse_data_dir(self):
        selected = QFileDialog.getExistingDirectory(self, "Choose CareerOS Data Directory", self.data_dir.text())
        if selected:
            self.data_dir.setText(selected); self.autosave_status.setText("New data folder selected. Click Copy data here... to migrate safely.")

    def import_resume(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose Original Resume", "", "Resume files (*.txt *.md *.pdf *.docx)")
        if not path: return
        try:
            extracted = self.resumes.import_original(path); self.refresh_resume_label(); self.saved.emit()
            QMessageBox.information(self, "Resume", f"Imported safely. Original preserved; local text extracted to:\n{extracted}")
        except Exception as exc:
            QMessageBox.critical(self, "Resume import", str(exc))

    def refresh_resume_label(self):
        source = self.resumes.imported_source_path()
        self.resume_label.setText(f"Selected: {source.name}\nStored safely in CareerOS." if source else "No resume selected yet. Choose a file above to begin.")

    def refresh_materials(self):
        self.materials.clear()
        for record in self.documents.db.supporting_documents():
            status = "AI-ready" if record["extraction_status"] == "ready" else "stored; not readable"
            item = QListWidgetItem(f"{record['original_name']}  -  {status} ({record['character_count']:,} chars)")
            item.setData(Qt.UserRole, record["id"]); self.materials.addItem(item)

    def update_search_summary(self):
        locations = [line.strip() for line in self.search_locations.toPlainText().splitlines() if line.strip()]
        queries = [line.strip() for line in self.search_queries.toPlainText().splitlines() if line.strip()]
        enabled_sites = sum(box.isChecked() for box in self.search_sites.values())
        self.search_summary.setText(f"{len(locations)} location(s) × {len(queries)} job type(s) × {enabled_sites} source(s) = {len(locations) * len(queries) * enabled_sites} searches per run")

    def update_match_weight_summary(self):
        rule = self.rule_weight.value(); ai = 100 - rule
        self.rule_weight_label.setText(f"Rule  {rule}%"); self.ai_weight_label.setText(f"AI  {ai}%")
        self.match_weight_summary.setText(f"Final = rule score × {rule}% + AI analysis × {ai}%")

    def sync_match_weight(self, _value):
        self.update_match_weight_summary(); self.schedule_autosave()

    def schedule_autosave(self, *_):
        self.autosave_status.setText("Saving automatically..."); self.autosave_timer.start(600)

    def _collect_settings(self) -> bool:
        api = self.settings.setdefault("api", {})
        self.settings["language"] = self.language.currentData()
        api["enabled"] = self.api_enabled.isChecked(); api["base_url"] = self.api_url.text().strip().rstrip("/"); api["model"] = self.api_model.text().strip()
        if self.api_key.text().strip(): api["encrypted_key"] = protect_secret(self.api_key.text().strip())
        chosen = self.model.currentText().strip(); self.settings["ai_mode"] = f"API: {api['model']}" if chosen == "API" else chosen; self.settings["fallback_enabled"] = self.fallback.isChecked()
        existing_models = self.settings.get("models", {})
        primary = chosen if chosen not in {"", "Auto", "API"} else str(existing_models.get("deep", ""))
        backup = next((model for model in self.detected_local_models if model != primary), str(existing_models.get("fast", "")))
        self.settings["models"] = {"deep": primary, "fast": backup}
        self.settings["profile"] = {key: field.text().strip() for key, field in self.profile_fields.items()}
        self.settings["resume_pdf"] = {"style": self.resume_pdf_style.currentText(), "font_size": self.resume_pdf_font_size.value(), "margins": self.resume_pdf_margins.currentText()}
        self.settings["match_weights"] = {"rule": self.rule_weight.value(), "ai": 100 - self.rule_weight.value()}
        locations = [line.strip() for line in self.search_locations.toPlainText().splitlines() if line.strip()]
        queries = [line.strip() for line in self.search_queries.toPlainText().splitlines() if line.strip()]
        sites = [key for key, box in self.search_sites.items() if box.isChecked()]
        if not locations or not queries or not sites:
            self.autosave_status.setText("Auto-save paused: keep at least one location, job type, and source.")
            return False
        self.settings.setdefault("search", {})["locations"] = list(dict.fromkeys(locations)); self.settings["search"]["queries"] = list(dict.fromkeys(queries)); self.settings["search"]["sites"] = sites; self.settings["search"]["distance"] = self.search_distance.value()
        return True

    def autosave(self):
        if not self._collect_settings(): return
        self.settings["data_dir"] = str(get_paths().data_dir.resolve()); save_settings(self.settings); self.ai.reload_settings(); self.saved.emit()
        if self.window().property("_careeros_language") != self.settings["language"]:
            localize_widget_tree(self.window(), self.settings["language"])
        self.autosave_status.setText("Saved automatically.")

    def add_materials(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add Supporting Files", "", "All files (*.*)")
        if not paths: return
        imported, unreadable = 0, []
        for path in paths:
            try:
                result = self.documents.import_file(path); imported += 1
                if result["status"] != "ready": unreadable.append(result["name"])
            except Exception as exc:
                unreadable.append(f"{Path(path).name}: {exc}")
        self.refresh_materials(); self.saved.emit()
        note = f"Added {imported} file(s)."
        if unreadable: note += "\n\nStored but not usable by AI:\n" + "\n".join(unreadable[:8])
        QMessageBox.information(self, "Additional Materials", note)

    def remove_material(self):
        item = self.materials.currentItem()
        if not item: return
        if QMessageBox.question(self, "Remove file", "Remove this stored additional file?") == QMessageBox.Yes:
            self.documents.remove(item.data(Qt.UserRole)); self.refresh_materials(); self.saved.emit()

    def migrate_data(self):
        if not self._collect_settings(): return
        selected_dir = str(Path(self.data_dir.text()).expanduser().resolve())
        current_dir = str(get_paths().data_dir.resolve())
        if selected_dir != current_dir:
            message = f"Copy all current CareerOS data to:\n{selected_dir}\n\nThe old folder will be kept as a backup. CareerOS must restart afterward."
            if QMessageBox.question(self, "Move data directory", message) != QMessageBox.Yes: return
            try:
                self.settings["data_dir"] = selected_dir; old, new = migrate_data_directory(self.settings, selected_dir)
            except Exception as exc:
                QMessageBox.critical(self, "Data directory", str(exc)); return
            QMessageBox.information(self, "Restart required", f"Data copied to:\n{new}\n\nOld data kept at:\n{old}\n\nClose and reopen CareerOS to use the new location.")
            return
        self.autosave()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle("CareerOS"); self.resize(1380, 850); self.setFont(QFont("Segoe UI Variable Text", 10)); self.setWindowIcon(QIcon(self._logo(36)))
        self.db = Database(); self.ai = AIManager(self.db); self.jobs_service = JobService(self.db, self.ai); self.resume_service = ResumeService(self.db, self.ai); self.document_service = SupportingDocumentService(self.db); self.cover_service = CoverLetterService(self.db, self.ai, self.resume_service)
        shell = QWidget(); shell.setObjectName("appShell"); self.setCentralWidget(shell); layout = QHBoxLayout(shell); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        sidebar = QWidget(); sidebar.setObjectName("sidebar"); sidebar_layout = QVBoxLayout(sidebar); sidebar_layout.setContentsMargins(12, 16, 12, 12); sidebar_layout.setSpacing(11); brand = QWidget(); brand.setObjectName("brand"); brand_layout = QHBoxLayout(brand); brand_layout.setContentsMargins(8, 4, 8, 8); brand_layout.setSpacing(9); brand_icon = QLabel(); brand_icon.setPixmap(self._logo(28)); brand_name = QLabel("<b>CareerOS</b><br/><span style='color:#64748b;font-size:10px'>Career workspace</span>"); brand_layout.addWidget(brand_icon); brand_layout.addWidget(brand_name); brand_layout.addStretch(); sidebar_layout.addWidget(brand)
        self.nav = QListWidget(); self.nav.setObjectName("navList"); self.nav.addItems(["Dashboard", "Jobs", "Resume & CV", "Applications", "Settings"]); self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); sidebar_layout.addWidget(self.nav); sidebar.setFixedWidth(208)
        right = QWidget(); right.setObjectName("rightPane"); right_layout = QVBoxLayout(right); right_layout.setContentsMargins(0, 0, 0, 0); right_layout.setSpacing(0); top_bar = QWidget(); top_bar.setObjectName("topBar"); top_bar.setFixedHeight(40); top_layout = QHBoxLayout(top_bar); top_layout.setContentsMargins(17, 0, 17, 0); top_label = QLabel("<b>CareerOS</b>"); top_label.setObjectName("topTitle"); top_layout.addWidget(top_label); top_layout.addStretch(); right_layout.addWidget(top_bar)
        self.stack = QStackedWidget(); self.dashboard = DashboardPage(self.db); self.jobs = JobsPage(self.db, self.jobs_service, self.resume_service, self.cover_service, self.ai); self.resume = ResumePage(self.db, self.resume_service, self.cover_service); self.applications = ApplicationsPage(self.db); self.settings = SettingsPage(self.resume_service, self.document_service, self.ai)
        for page in (self.dashboard, self.jobs, self.resume, self.applications, self.settings): self.stack.addWidget(page)
        self.nav.currentRowChanged.connect(self.change_page); self.jobs.changed.connect(self.refresh_pages); self.jobs.resume_ready.connect(lambda: self.nav.setCurrentRow(2)); self.settings.saved.connect(self.settings_changed); right_layout.addWidget(self.stack); layout.addWidget(sidebar); layout.addWidget(right); self._configure_combo_popups(); self.wheel_focus_guard = WheelFocusGuard(self)
        for widget in self.findChildren(QComboBox):
            widget.installEventFilter(self.wheel_focus_guard)
        for widget in self.findChildren(QSpinBox):
            widget.installEventFilter(self.wheel_focus_guard)
            widget.lineEdit().installEventFilter(self.wheel_focus_guard)
            widget.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        for widget in self.findChildren(QSlider):
            widget.installEventFilter(self.wheel_focus_guard)
        self.nav.setCurrentRow(0); localize_widget_tree(self)
        check_icon = (Path(__file__).resolve().parents[1] / "assets" / "check.svg").as_posix()
        self.setStyleSheet("""
            QWidget{font-family:'Segoe UI Variable Text','Segoe UI';font-size:13px;color:#1d1d1f;background:#f6f7fb}
            QLabel{background:transparent} QMainWindow{background:#f6f7fb} QWidget#appShell{background:#f6f7fb;border-radius:12px}
            QWidget#sidebar{background:#e1e5ee;border-top-left-radius:12px;border-bottom-left-radius:12px;border-right:1px solid #cbd1dc}
            QWidget#brand{background:transparent} QWidget#rightPane{background:#f5f5f7;border-top-right-radius:12px;border-bottom-right-radius:12px}
            QWidget#topBar{background:#e8ecf4;border-top-right-radius:12px;border-bottom:1px solid #d4d9e2} QLabel#topTitle{background:transparent;color:#26354b;font-family:'Segoe UI Variable Display','Segoe UI';font-size:14px}
            QListWidget#navList{padding:3px 0;border:none;background:transparent;outline:none}
            QListWidget#navList::item{padding:10px 12px;margin:2px 0;border-radius:9px;color:#27303d}
            QListWidget#navList::item:hover{background:rgba(207,215,229,195)} QListWidget#navList::item:selected{background:#d6e8ff;color:#0969d9;font-weight:600}
            QListWidget#versionList{background:rgba(255,255,255,220);border:1px solid rgba(210,210,215,190);border-radius:9px;padding:5px;outline:none}
            QListWidget#versionList::item{color:#1d1d1f;padding:7px 9px;margin:1px;border-radius:6px}
            QListWidget#versionList::item:hover{background:#f0f5ff} QListWidget#versionList::item:selected{background:#d9e9ff;color:#0a60c8}
            QPushButton{min-height:18px;padding:7px 14px;border:1px solid #d7dbe3;border-radius:9px;background:#ffffff;font-weight:500;color:#253044}
            QPushButton:hover{background:#f7faff;border-color:#88b9ff} QPushButton:pressed{background:#e5f0ff} QPushButton:disabled{color:#9a9aa0;background:#f1f1f3;border-color:#e0e0e3}
            QLineEdit,QComboBox,QTextEdit,QTableWidget,QSpinBox{border:1px solid #d7dbe3;border-radius:9px;padding:6px 9px;background:#ffffff;color:#1d1d1f;selection-background-color:#cfe3ff}
            QLineEdit:focus,QComboBox:focus,QTextEdit:focus,QSpinBox:focus{border:1px solid #0a84ff}
            QComboBox{padding-right:27px} QComboBox::drop-down{border:none;width:25px} QComboBox QAbstractItemView,QComboBox QAbstractItemView::viewport{background:#ffffff;border:1px solid #c7cbd4;border-radius:7px;padding:4px;selection-background-color:#d9e9ff;outline:none;color:#1d1d1f} QComboBox QAbstractItemView::item{padding:6px 8px;background:#ffffff;color:#1d1d1f} QComboBox QAbstractItemView::item:selected{background:#d9e9ff;color:#0a60c8}
            QHeaderView::section{padding:9px;background:#f6f7fb;color:#687386;border:none;border-bottom:1px solid #d7dbe3;font-weight:600}
            QTableWidget{gridline-color:#e7e9ee;selection-background-color:#dcecff;selection-color:#1d1d1f;alternate-background-color:#fbfcfe}
            QTabWidget::pane{border:1px solid rgba(210,210,215,205);border-radius:9px;background:rgba(255,255,255,218);top:-1px}
            QTabBar::tab{background:rgba(236,236,240,190);border:1px solid rgba(210,210,215,205);border-bottom:none;border-top-left-radius:8px;border-top-right-radius:8px;padding:8px 13px;margin-right:3px;color:#3a3a3c}
            QTabBar::tab:selected{background:rgba(255,255,255,235);color:#0a60c8;font-weight:600}
            QLabel#statsCard{padding:18px;border:1px solid rgba(210,210,215,205);border-radius:12px;background:rgba(255,255,255,215)}
            QWidget#metricCard,QWidget#dashboardPanel,QWidget#settingsCard{background:#ffffff;border:1px solid #dde1e8;border-radius:13px}
            QLabel#metricValue{background:transparent;color:#172f52;font-family:'Segoe UI Variable Display','Segoe UI';font-size:23px;font-weight:700}
            QLabel#metricCaption{background:transparent;color:#6e7786;font-size:11px}
            QListWidget#recentList{background:transparent;border:none;outline:none;padding:0}
            QListWidget#recentList::item{background:transparent;border-bottom:1px solid #edf0f4;padding:0;margin:0}
            QListWidget#recentList::item:last{border-bottom:none} QWidget#recentRow{background:transparent}
            QWidget#settingsCard QLabel,QWidget#apiSettingsCard QLabel{background:transparent} QWidget#settingsCard QLineEdit,QWidget#settingsCard QComboBox,QWidget#settingsCard QTextEdit,QWidget#settingsCard QSpinBox,QWidget#apiSettingsCard QLineEdit{background:#fbfcfe}
            QWidget#apiSettingsCard{background:#ffffff;border:1px solid #dde1e8;border-radius:13px}
            QWidget#apiSettingsCard[apiFieldsDisabled="true"]{background:#eceef2;border-color:#d7dbe3} QWidget#apiSettingsCard[apiFieldsDisabled="true"] QLabel{color:#8b93a1} QWidget#apiSettingsCard[apiFieldsDisabled="true"] QLineEdit{background:#e5e7eb;color:#9aa1ad;border-color:#d7dbe3}
            QLabel#versionLabel{color:#94a0b1;font-size:11px;padding:8px 0 2px}
            QSlider::groove:horizontal{height:7px;background:#d8dee8;border-radius:4px} QSlider::sub-page:horizontal{background:#4e8df7;border-radius:4px} QSlider::add-page:horizontal{background:#d8dee8;border-radius:4px} QSlider::handle:horizontal{width:17px;margin:-5px 0;border-radius:9px;background:#ffffff;border:2px solid #2878e8}
            QCheckBox{background:transparent;color:#394150;spacing:7px} QCheckBox::indicator{width:16px;height:16px;border:1px solid #bfc6d2;border-radius:4px;background:#ffffff} QCheckBox::indicator:checked{background:#2878e8;border-color:#2878e8;image:url(__CHECK_ICON__)}
            QScrollArea,QScrollArea::viewport{border:none;background:#f6f7fb} QScrollBar:vertical{background:transparent;width:10px;margin:4px} QScrollBar::handle:vertical{background:#bdc4d0;border-radius:5px;min-height:28px} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0}
        """.replace("__CHECK_ICON__", check_icon))
        QTimer.singleShot(0, self._apply_windows_backdrop)

    def closeEvent(self, event):
        scan = getattr(self.settings, "model_scan", None)
        if scan and scan.isRunning():
            scan.wait(3500)
        super().closeEvent(event)

    def _apply_windows_backdrop(self):
        """Use Windows 11 Mica and rounded corners where Windows exposes them; CSS is the fallback."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            hwnd = int(self.winId())
            dwm = ctypes.windll.dwmapi
            light_title_bar = ctypes.c_int(0); mica = ctypes.c_int(2); rounded = ctypes.c_int(2)
            dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(light_title_bar), ctypes.sizeof(light_title_bar))
            dwm.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(rounded), ctypes.sizeof(rounded))
            dwm.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(mica), ctypes.sizeof(mica))
        except Exception:
            pass

    @staticmethod
    def _logo(size: int) -> QPixmap:
        pixmap = QPixmap(size, size); pixmap.fill(Qt.transparent); painter = QPainter(pixmap); painter.setRenderHint(QPainter.Antialiasing)
        gradient = QLinearGradient(0, 0, size, size); gradient.setColorAt(0, QColor("#2f80ed")); gradient.setColorAt(1, QColor("#7559d9")); painter.setBrush(gradient); painter.setPen(Qt.NoPen); painter.drawRoundedRect(1, 1, size - 2, size - 2, size * 0.26, size * 0.26)
        painter.setPen(QColor("#ffffff")); painter.setBrush(Qt.NoBrush); painter.drawEllipse(int(size * .25), int(size * .24), int(size * .50), int(size * .50)); painter.drawLine(int(size * .52), int(size * .32), int(size * .72), int(size * .52)); painter.drawLine(int(size * .52), int(size * .68), int(size * .72), int(size * .48)); painter.end(); return pixmap

    def _configure_combo_popups(self):
        popup_style = "QFrame{background:#ffffff;border:1px solid #c7cbd4;border-radius:7px;} QAbstractItemView,QAbstractItemView::viewport{background:#ffffff;border:none;color:#1d1d1f;outline:0;} QAbstractItemView::item{background:#ffffff;color:#1d1d1f;padding:6px 8px;} QAbstractItemView::item:selected{background:#d9e9ff;color:#0a60c8;}"
        for combo in self.findChildren(QComboBox):
            popup = combo.view().window(); popup.setWindowFlag(Qt.FramelessWindowHint, True); popup.setAttribute(Qt.WA_StyledBackground, True); popup.setStyleSheet(popup_style)

    def change_page(self, index):
        self.stack.setCurrentIndex(index); page = self.stack.currentWidget(); effect = QGraphicsOpacityEffect(page); page.setGraphicsEffect(effect); animation = QPropertyAnimation(effect, b"opacity", self); animation.setDuration(170); animation.setStartValue(0.72); animation.setEndValue(1.0); animation.setEasingCurve(QEasingCurve.OutCubic); animation.finished.connect(lambda: page.setGraphicsEffect(None)); self.page_animation = animation; animation.start(); self.refresh_pages()
    def refresh_pages(self): self.dashboard.refresh(); self.jobs.refresh(); self.resume.refresh(); self.applications.refresh()
    def settings_changed(self): self.jobs.refresh_models(); self.jobs.refresh_location_choices(); self.refresh_pages()


def _startup_splash() -> QSplashScreen:
    """Create a brief branded loading window before the main workspace is built."""
    size = QSize(768, 432)
    image = QPixmap(str(Path(__file__).resolve().parents[1] / "assets" / "splash-background.jpg"))
    canvas = QPixmap(size); canvas.fill(QColor("#14365d"))
    painter = QPainter(canvas); painter.setRenderHint(QPainter.SmoothPixmapTransform)
    if not image.isNull():
        scaled = image.scaled(size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        painter.drawPixmap((size.width() - scaled.width()) // 2, (size.height() - scaled.height()) // 2, scaled)
    painter.fillRect(canvas.rect(), QColor(4, 22, 47, 42))
    lower_top = int(size.height() * 0.72); margin = int(size.width() * 0.044)
    painter.fillRect(QRect(0, lower_top, size.width(), size.height() - lower_top), QColor(5, 20, 39, 178))
    painter.setPen(QColor("#ffffff")); painter.setFont(QFont("Segoe UI Variable Display", 23, QFont.DemiBold))
    painter.drawText(QRect(margin, lower_top + 16, 500, 42), Qt.AlignLeft | Qt.AlignVCenter, "CareerOS")
    painter.setPen(QColor("#d9e9ff")); painter.setFont(QFont("Segoe UI", 10))
    language = load_settings().get("language", "en")
    message = "正在加载本地求职工作区…" if language == "zh" else "Loading your local career workspace…"
    painter.drawText(QRect(margin, lower_top + 59, 600, 26), Qt.AlignLeft | Qt.AlignVCenter, message)
    painter.end()
    splash = QSplashScreen(canvas, Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
    loading_bar = QProgressBar(splash); loading_bar.setRange(0, 0); loading_bar.setTextVisible(False)
    loading_bar.setGeometry(margin, lower_top + 96, 216, 6)
    loading_bar.setStyleSheet("QProgressBar{border:none;border-radius:3px;background:rgba(255,255,255,55)} QProgressBar::chunk{border-radius:3px;background:#7db6ff}")
    splash.loading_bar = loading_bar
    return splash


def run_gui():
    app = QApplication.instance() or QApplication([]); app.setStyle("Fusion")
    elapsed = QElapsedTimer(); elapsed.start()
    splash = _startup_splash(); splash.show(); app.processEvents()
    window = MainWindow()
    remaining = max(0, 3000 - elapsed.elapsed())
    if remaining:
        wait_loop = QEventLoop(); QTimer.singleShot(remaining, wait_loop.quit); wait_loop.exec()
    window.show(); splash.finish(window)
    return app.exec()
