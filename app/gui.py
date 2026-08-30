from __future__ import annotations

import copy
import difflib
import hashlib
import html
import json
import logging
import re
import sys
import time
import unicodedata
from uuid import uuid4
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QElapsedTimer, QEvent, QEventLoop, QObject, QPropertyAnimation, QRect, QSize, QThread, QTimer, Signal, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QLinearGradient, QPainter, QPixmap
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QAbstractItemView, QAbstractSpinBox, QGraphicsOpacityEffect, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTabWidget, QTextBrowser, QTextEdit, QVBoxLayout, QWidget, QSizePolicy, QSplashScreen, QMenu,
)

from app.ai_manager import AIManager
from app.database import Database
from app.docx_export import export_docx_pdf
from app.form_fill import CONTACT_FIELDS, build_form_fill_script, form_fill_values
from app.i18n import localize_widget_tree, translate
from app.pdf_export import render_resume_document_pdf
from app.resume_models import ExperienceItem, EducationItem, LanguageItem, ProjectItem, ResumeBullet, ResumeSection, SkillGroup, resume_document_from_dict, resume_document_to_dict, resume_layout_to_dict, resume_style_to_dict
from app.resume_parser import parse_resume_text, resume_document_to_text
from app.resume_integrity import ResumeIntegrityValidator
from app.secrets import protect_secret
from app.services import JobService, ResumeService, SupportingDocumentService, compact_job_text, evaluate_requirement, extract_job_facts
from config import get_paths, load_settings, migrate_data_directory, save_settings


# Keep the workflow intentionally small: save an opportunity, mark it as an
# interest, then track only real application milestones.
STATUSES = ["New", "Interested", "Applied", "Interview", "Offer", "Ignored"]
MAX_BATCH_JOBS = 20
SEARCH_SITES = {
    "indeed": "Indeed",
    "linkedin": "LinkedIn",
    "google": "Google Jobs",
    "glassdoor": "Glassdoor",
    "zip_recruiter": "ZipRecruiter",
}
logger = logging.getLogger(__name__)


def user_error_message(value: object, fallback: str = "The operation could not be completed.") -> str:
    """Remove traceback frames/internal source paths from GUI error text."""
    text = str(value or "").replace("\r", "").strip()
    if not text:
        return fallback
    text = re.split(r"\n\s*(?:Traceback \(most recent call last\):|File \"[^\"]+\", line \d+)", text, maxsplit=1)[0].strip()
    lines = [line.strip() for line in text.splitlines() if line.strip() and not re.match(r'^(?:File \".*\", line \d+|at .+\.py:\d+)', line.strip())]
    clean = "\n".join(lines).strip()
    return (clean[:700] + "…") if len(clean) > 700 else (clean or fallback)


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
            result = self.func(*self.args, self.progress.emit, self.isInterruptionRequested) if self.with_progress else self.func(*self.args)
            self.completed.emit(result)
        except Exception as exc:
            logger.exception("Background task failed")
            # Detailed diagnostics are logged above. GUI callers receive only a
            # concise message so internal paths and tracebacks never leak.
            self.failed.emit(user_error_message(exc, "The background task could not be completed."))


class CollapsibleSection(QWidget):
    """CareerOS-style disclosure card: click the header row to expand/collapse."""

    def __init__(self, title: str, parent=None, *, expanded: bool = False):
        super().__init__(parent)
        self._title = title
        self.setObjectName("editorDisclosure")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.header = QPushButton()
        self.header.setObjectName("editorDisclosureHeader")
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setMinimumHeight(40)
        outer.addWidget(self.header)
        self._content = QWidget(self)
        self._content.setObjectName("editorDisclosureBody")
        self.content_layout = QVBoxLayout(self._content)
        self.content_layout.setContentsMargins(12, 10, 12, 12)
        self.content_layout.setSpacing(8)
        outer.addWidget(self._content)
        self.header.toggled.connect(self._set_expanded)
        self._set_expanded(expanded)

    def _set_expanded(self, expanded: bool):
        self._content.setVisible(expanded)
        self.header.setText(("▾  " if expanded else "▸  ") + self._title)

    def setFocus(self, reason=Qt.OtherFocusReason):
        self.header.setFocus(reason)


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

    def __init__(self, db: Database, jobs: JobService, resumes: ResumeService, ai: AIManager):
        super().__init__(); self.db, self.service, self.resumes, self.ai = db, jobs, resumes, ai; self.current_job = None; self.worker = None; self.task_started_at = None
        root = QVBoxLayout(self); root.setContentsMargins(22, 20, 22, 22); root.setSpacing(10); title_label = QLabel("<h1>Jobs</h1><p style='color:#6e6e73;margin-top:-7px'>Search, review and organize opportunities.</p>"); title_label.setMinimumHeight(74); root.addWidget(title_label)
        controls = QHBoxLayout(); self.search_box = QLineEdit(); self.search_box.setPlaceholderText("Search company, position, location"); self.search_box.textChanged.connect(self.refresh)
        self.score_filter = QComboBox(); self.score_filter.addItem("All matches", ""); self.score_filter.addItem("75+ Good", "75"); self.score_filter.addItem("60+ Possible", "60"); self.score_filter.addItem("Unscored", "unscored"); self.score_filter.currentTextChanged.connect(self.refresh)
        self.status_filter = QComboBox(); self.status_filter.addItem("All statuses", ""); [self.status_filter.addItem(status, status) for status in STATUSES]; self.status_filter.currentTextChanged.connect(self.refresh)
        self.location_filter = QComboBox(); self.location_filter.addItem("All locations", ""); [self.location_filter.addItem(location, location) for location in load_settings().get("search", {}).get("locations", [])]; self.location_filter.currentTextChanged.connect(self.refresh)
        self.source_filter = QComboBox(); self.source_filter.addItem("All sources", "")
        for key, label in SEARCH_SITES.items(): self.source_filter.addItem(label, key)
        self.source_filter.addItem("Manual", "manual"); self.source_filter.addItem("Import", "import"); self.source_filter.currentTextChanged.connect(self.refresh)
        self.model = QComboBox(); self.refresh_models(); self.model.hide()
        search_btn = QPushButton("Search Jobs"); search_btn.clicked.connect(self.run_search); add_btn = QPushButton("Add Job"); add_btn.clicked.connect(self.add_job); import_btn = QPushButton("Import"); import_btn.clicked.connect(self.import_jobs)
        for w in (self.search_box, self.score_filter, self.status_filter, self.location_filter, self.source_filter, search_btn, add_btn, import_btn): controls.addWidget(w)
        root.addLayout(controls)
        selection_hint = QLabel("Tip: use Ctrl or Shift to select multiple jobs (maximum 20 per batch)."); root.addWidget(selection_hint)
        split = QSplitter(); self.table = QTableWidget(0, 7); self.table.setHorizontalHeaderLabels(["Company", "Match", "Position", "Location", "Source", "Salary", "Status"]); self.table.horizontalHeader().setMinimumSectionSize(75)
        for column in (1, 4, 5, 6): self.table.horizontalHeaderItem(column).setTextAlignment(Qt.AlignCenter)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setSelectionMode(QAbstractItemView.ExtendedSelection); self.table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.table.setSortingEnabled(True); self.table.itemSelectionChanged.connect(self.show_selected)
        detail = QWidget(); dl = QVBoxLayout(detail); self.detail_title = QLabel("Select a job"); self.detail_title.setWordWrap(True); self.detail = QTextEdit(); self.detail.setReadOnly(True); self.description = QTextEdit(); self.description.setReadOnly(True); self.detail_tabs = QTabWidget(); self.detail_tabs.addTab(self.detail, "Requirements && Match"); self.detail_tabs.addTab(self.description, "Full Description"); self.status = QComboBox(); self.status.addItems(STATUSES); self.status.currentTextChanged.connect(self.change_status)
        actions = QHBoxLayout(); analyze = QPushButton("Analyze Selected"); analyze.clicked.connect(self.analyze); interested = QPushButton("Interested"); interested.clicked.connect(lambda: self.set_selected_status("Interested")); ignored = QPushButton("Ignored"); ignored.clicked.connect(lambda: self.set_selected_status("Ignored")); open_btn = QPushButton("Open Job Page"); open_btn.clicked.connect(self.open_job); fill_btn = QPushButton("Copy Form Fill Script"); fill_btn.clicked.connect(self.copy_form_fill_script)
        for w in (analyze, interested, ignored, open_btn, fill_btn): actions.addWidget(w)
        translation_controls = QHBoxLayout(); self.translate_button = QPushButton("Translate 中文"); self.translate_button.clicked.connect(self.translate_description); self.show_chinese = QCheckBox("Show Chinese"); self.show_chinese.setEnabled(False); self.show_chinese.stateChanged.connect(self.toggle_translation); self.translation_status = QLabel("Original text"); translation_controls.addWidget(self.translate_button); translation_controls.addWidget(self.show_chinese); translation_controls.addWidget(self.translation_status); translation_controls.addStretch()
        dl.addWidget(self.detail_title); dl.addWidget(self.status); dl.addLayout(actions); dl.addLayout(translation_controls); dl.addWidget(self.detail_tabs)
        split.addWidget(self.table); split.addWidget(detail); split.setSizes([650, 650]); root.addWidget(split); root.setStretch(3, 1)
        self.progress = QLabel("AI: Ready" if ai.available_models() else "AI: Offline")
        root.addWidget(self.progress); self.refresh()

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
        if not str(load_settings().get("ai_mode", "Auto")).startswith("API:"):
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
        shell = self.window()
        if hasattr(shell, "update_task_state"):
            shell.update_task_state(translate(text), self.task_started_at)
        match = re.search(r"(?:Searching|Analyzing|Resume|Cover letter) (\d+)/(\d+):", text)
        if not match or not self.task_started_at:
            return
        index, total = map(int, match.groups()); completed = max(0, index - 1); elapsed = time.monotonic() - self.task_started_at
        if completed:
            remaining = elapsed / completed * (total - completed)
            detail = translate(f"Progress {completed}/{total} · elapsed {self._duration(elapsed)} · about {self._duration(remaining)} remaining")
        else:
            detail = translate(f"Progress 0/{total} · estimating time remaining...")
        if hasattr(shell, "set_task_progress"):
            shell.set_task_progress(completed, total, detail)

    def finish_task_progress(self):
        shell = self.window()
        elapsed = time.monotonic() - self.task_started_at if self.task_started_at else 0
        if hasattr(shell, "finish_task_state"):
            shell.finish_task_state(translate(f"Completed in {self._duration(elapsed)}"))
        self.task_started_at = None

    def fail(self, text):
        self.finish_task_progress(); self.progress.setText(translate("Error")); QMessageBox.critical(self, "CareerOS", user_error_message(text))
    def run_task(self, func, done, *args, with_progress=False):
        if self.worker and self.worker.isRunning():
            self.busy("Another task is still running")
            QMessageBox.information(self, "CareerOS", "Another search or AI task is still running.")
            return False
        self.task_started_at = time.monotonic()
        self.worker = TaskThread(func, *args, with_progress=with_progress)
        shell = self.window()
        if hasattr(shell, "begin_task_state"):
            shell.begin_task_state(self.worker, translate("Starting task..."))
        def completed(result):
            done(result); self.finish_task_progress()
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

    def _analyze_many(self, jobs, resume, model, progress, cancelled):
        completed, errors, models = 0, [], []
        for index, job in enumerate(jobs, 1):
            if cancelled(): break
            progress(f"Analyzing {index}/{len(jobs)}: {job['company']} — {job['title']}")
            try:
                result = self.service.analyze(job["id"], resume, model); completed += 1; models.append(result["model_used"])
            except Exception as exc:
                errors.append(f"{job['company']} — {job['title']}: {user_error_message(exc)}")
        return {"completed": completed, "errors": errors, "models": list(dict.fromkeys(models)), "cancelled": cancelled()}

    def _optimize_many(self, jobs, model, progress, cancelled):
        completed, errors, versions = 0, [], []
        for index, job in enumerate(jobs, 1):
            if cancelled(): break
            progress(f"Resume {index}/{len(jobs)}: {job['company']} — {job['title']}")
            try:
                result = self.resumes.optimize(job, model); completed += 1; versions.append(result["version_name"])
            except Exception as exc:
                errors.append(f"{job['company']} — {job['title']}: {user_error_message(exc)}")
        return {"completed": completed, "errors": errors, "versions": versions, "cancelled": cancelled()}

    def show_batch_result(self, title: str, result: dict):
        errors = result.get("errors", [])
        message = f"Completed: {result.get('completed', 0)}\nErrors: {len(errors)}"
        if errors:
            message += "\n\n" + "\n".join(errors[:8])
        QMessageBox.information(self, title, message)

    def run_search(self):
        self.busy("Searching...")
        self.run_task(self.service.search, lambda r: (self.busy("Search cancelled" if r.get("cancelled") else f"Search complete: {r['added']} new, {r['existing']} existing, {r['errors']} warnings"), self.refresh(), self.changed.emit()), with_progress=True)

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
        self.busy(f"Analyzing {len(jobs)} selected job(s)..."); model = None
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
        self.run_task(self._optimize_many, done, jobs, None, with_progress=True)

    def translate_description(self):
        if not self.current_job: return
        job_id = self.current_job["id"]
        self.busy("Translating to Chinese..."); self.translate_button.setEnabled(False)
        def done(result):
            translated_job = self.db.job(job_id); self.translate_button.setEnabled(True)
            if result.get("cancelled"):
                self.busy("Translation cancelled")
                return
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

    def set_selected_status(self, status: str):
        jobs = self.selected_jobs()
        if not jobs:
            QMessageBox.information(self, "CareerOS", "Select at least one job first.")
            return
        for job in jobs:
            self.db.update_job(job["id"], status=status)
        if self.current_job and any(job["id"] == self.current_job["id"] for job in jobs):
            self.current_job["status"] = status
        self.busy(f"Marked {len(jobs)} job(s) as {status}")
        self.refresh(); self.changed.emit()

class ResumeEditorWindow(QMainWindow):
    """Structured, non-modal Resume editor with an exact-page PDF preview."""
    about_to_save = Signal(int)
    saved = Signal(int)

    def __init__(self, service: ResumeService, version: dict, parent=None):
        super().__init__(parent)
        self.service = service
        self.version_id = int(version["id"])
        self.version_name = str(version.get("version_name") or "Resume")
        self.document_type = str(version.get("document_type") or "Resume")
        self.document = copy.deepcopy(service.document_from_version(version))
        self._normalize_section_order()
        self.style = copy.deepcopy(service.style_from_version(version))
        self.layout_model = copy.deepcopy(service.layout_from_version(version))
        # CareerOS does not expose a Summary editor in V2.  Keep any stored
        # summary data non-destructively, but hide it from editor/output by
        # default.  Future AI generations are also instructed not to create one.
        if "summary" not in self.layout_model.hidden_sections:
            self.layout_model.hidden_sections.append("summary")
        self._initial_json = self._snapshot_json()
        self._history = [self._initial_json]
        self._history_index = 0
        self._restoring = False
        self._anchor_widgets: dict[str, QWidget] = {}
        self._expanded_sections: dict[str, bool] = {}
        self._editor_container: QWidget | None = None
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        # Exact preview renders a real PDF after the user stops typing.  A
        # longer debounce prevents Word automation from running per keystroke.
        self._preview_timer.setInterval(700)
        self._preview_timer.timeout.connect(self.refresh_preview)
        self._preview_worker = None
        self._preview_pending = False
        self._preview_generation = 0
        self._preview_path: Path | None = None
        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.setInterval(350)
        self._history_timer.timeout.connect(self._record_snapshot)
        self.setWindowTitle(f"{self.document_type} Editor — {self.version_name}")
        self.setFont(QFont("Segoe UI Variable Text", 10))
        icon_path = Path(__file__).resolve().parents[1] / "assets" / "careeros.ico"
        if icon_path.is_file():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1460, 900)
        self.setMinimumSize(1050, 700)
        self._build_ui()
        self._rebuild_editor()
        self.refresh_preview()
        self._update_action_state()

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("resumeEditorShell")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        toolbar = QWidget()
        toolbar.setObjectName("resumeEditorToolbar")
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(12, 8, 12, 8)
        title = QLabel(f"<b>{html.escape(self.document_type)} Editor</b> &nbsp; <span style='color:#6e7786'>{html.escape(self.version_name)}</span>")
        title.setObjectName("editorTitle")
        bar.addWidget(title)
        bar.addStretch()
        self.undo_button = QPushButton("Undo")
        self.redo_button = QPushButton("Redo")
        self.zoom_out = QPushButton("−")
        self.zoom_in = QPushButton("+")
        self.save_button = QPushButton("Save Changes")
        self.close_button = QPushButton("Close")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        self.zoom_out.clicked.connect(lambda: self._adjust_pdf_zoom(0.90))
        self.zoom_in.clicked.connect(lambda: self._adjust_pdf_zoom(1.10))
        self.save_button.clicked.connect(self.save_changes)
        self.close_button.clicked.connect(self.close)
        for widget in (self.undo_button, self.redo_button):
            bar.addWidget(widget)
        bar.addWidget(QLabel("Preview"))
        bar.addWidget(self.zoom_out)
        bar.addWidget(self.zoom_in)
        bar.addSpacing(8)
        bar.addWidget(self.save_button)
        bar.addWidget(self.close_button)
        layout.addWidget(toolbar)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        layout.addWidget(split, 1)
        self.editor_scroll = QScrollArea()
        self.editor_scroll.setWidgetResizable(True)
        self.editor_scroll.setObjectName("resumeEditorScroll")
        split.addWidget(self.editor_scroll)
        preview_host = QWidget()
        preview_layout = QVBoxLayout(preview_host)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(4)
        self.preview_status = QLabel("Exact PDF preview")
        self.preview_status.setStyleSheet("color:#6e7786;font-size:11px")
        self.preview_pdf = QPdfDocument(self)
        self.preview = QPdfView()
        self.preview.setDocument(self.preview_pdf)
        self.preview.setPageMode(QPdfView.PageMode.MultiPage)
        self.preview.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.preview.setStyleSheet("QPdfView{background:#eef1f5;border:1px solid #d8dde6;border-radius:8px}")
        preview_layout.addWidget(self.preview_status)
        preview_layout.addWidget(self.preview, 1)
        split.addWidget(preview_host)
        split.setSizes([560, 900])
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        self.setStyleSheet("""
            QMainWindow, QWidget#resumeEditorShell { background:#f6f7fb; color:#1d1d1f; font-family:'Segoe UI Variable Text','Segoe UI'; font-size:13px; }
            QWidget#resumeEditorToolbar { background:#ffffff; border:1px solid #dde1e8; border-radius:12px; }
            QLabel#editorTitle { background:transparent; color:#26354b; font-size:14px; }
            QScrollArea#resumeEditorScroll, QScrollArea#resumeEditorScroll > QWidget > QWidget { background:#f6f7fb; border:none; }
            QWidget#editorDisclosure { background:#ffffff; border:1px solid #dde1e8; border-radius:11px; }
            QPushButton#editorDisclosureHeader { text-align:left; padding:9px 12px; border:none; border-radius:10px; background:#ffffff; color:#27303d; font-weight:600; }
            QPushButton#editorDisclosureHeader:hover { background:#f0f5ff; color:#0a60c8; }
            QPushButton#editorDisclosureHeader:checked { background:#eef5ff; color:#0a60c8; border-bottom-left-radius:0; border-bottom-right-radius:0; }
            QWidget#editorDisclosureBody { background:#ffffff; border-top:1px solid #edf0f4; }
            QPushButton { min-height:18px; padding:7px 14px; border:1px solid #d7dbe3; border-radius:9px; background:#ffffff; font-weight:500; color:#253044; }
            QPushButton:hover { background:#f7faff; border-color:#88b9ff; }
            QPushButton:pressed { background:#e5f0ff; }
            QPushButton:disabled { color:#9a9aa0; background:#f1f1f3; border-color:#e0e0e3; }
            QLineEdit,QTextEdit,QListWidget { border:1px solid #d7dbe3; border-radius:9px; padding:6px 9px; background:#fbfcfe; color:#1d1d1f; selection-background-color:#cfe3ff; }
            QLineEdit:focus,QTextEdit:focus,QListWidget:focus { border:1px solid #0a84ff; }
            QGroupBox { background:#fbfcfe; border:1px solid #e1e5ec; border-radius:9px; margin-top:10px; padding-top:8px; font-weight:600; }
            QGroupBox::title { subcontrol-origin:margin; left:10px; padding:0 4px; color:#334155; }
            QLabel { background:transparent; color:#253044; }
            QGroupBox QLabel { color:#253044; }
            QPdfView { background:#e9edf3; border:1px solid #d8dde6; border-radius:10px; }
            QScrollBar:vertical { background:transparent; width:10px; margin:4px; }
            QScrollBar::handle:vertical { background:#bdc4d0; border-radius:5px; min-height:28px; }
            QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical { height:0; }
        """)

    def _snapshot_json(self) -> str:
        return json.dumps(resume_document_to_dict(self.document), sort_keys=True, ensure_ascii=False)

    def _record_snapshot(self):
        if self._restoring:
            return
        snap = self._snapshot_json()
        if self._history and self._history[self._history_index] == snap:
            return
        self._history = self._history[:self._history_index + 1]
        self._history.append(snap)
        self._history_index += 1
        if len(self._history) > 80:
            self._history.pop(0)
            self._history_index -= 1
        self._update_action_state()

    def _changed(self, immediate: bool = False):
        if self._restoring:
            return
        self._preview_timer.start()
        if immediate:
            self._record_snapshot()
        else:
            self._history_timer.start()
        self._update_action_state()

    def _update_action_state(self):
        self.undo_button.setEnabled(self._history_index > 0)
        self.redo_button.setEnabled(self._history_index < len(self._history) - 1)
        self.save_button.setEnabled(self._snapshot_json() != self._initial_json)

    def undo(self):
        self._record_snapshot()
        if self._history_index <= 0:
            return
        self._history_index -= 1
        self._restore_snapshot(self._history[self._history_index])

    def redo(self):
        if self._history_index >= len(self._history) - 1:
            return
        self._history_index += 1
        self._restore_snapshot(self._history[self._history_index])

    def _restore_snapshot(self, payload: str):
        self._restoring = True
        try:
            self.document = resume_document_from_dict(json.loads(payload))
            self._rebuild_editor()
            self.refresh_preview()
        finally:
            self._restoring = False
            self._update_action_state()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}-user-{uuid4().hex[:8]}"

    def _rebuild_editor(self):
        old_scroll = self.editor_scroll.verticalScrollBar().value() if self._editor_container else 0
        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(8, 8, 8, 12)
        outer.setSpacing(10)
        self._editor_container = container
        self._anchor_widgets = {}
        outer.addWidget(QLabel("<b>Structured content</b><br/><span style='color:#6e6e73;font-size:11px'>Edit fields directly. Changes stay in a working copy until Save Changes.</span>"))
        self._build_personal(outer)
        self._build_sections(outer)
        self._build_education(outer)
        self._build_experience(outer)
        self._build_projects(outer)
        self._build_skills(outer)
        self._build_languages(outer)
        self._build_certifications(outer)
        self._build_custom(outer)
        outer.addStretch()
        self.editor_scroll.setWidget(container)
        QTimer.singleShot(0, lambda: self.editor_scroll.verticalScrollBar().setValue(old_scroll))

    def _register(self, widget: QWidget, anchor: str):
        widget.setProperty("_resume_anchor", anchor)
        widget.installEventFilter(self)
        self._anchor_widgets.setdefault(anchor, widget)
        return widget

    def eventFilter(self, watched, event):
        # The PDF preview is intentionally a true rendered page, so it does not
        # expose HTML anchors.  Keep focus handling local to the editor.
        return super().eventFilter(watched, event)

    def _section_box(self, title: str, anchor: str):
        # All structured sections start collapsed.  Long resumes stay easy to
        # scan, and the user opens only the section currently being edited.
        box = CollapsibleSection(title, expanded=self._expanded_sections.get(anchor, False))
        box.header.toggled.connect(lambda open_, key=anchor: self._expanded_sections.__setitem__(key, open_))
        self._anchor_widgets[anchor] = box
        return box, box.content_layout

    def _line(self, form: QFormLayout, label: str, obj, attr: str, anchor: str):
        field = self._register(QLineEdit(str(getattr(obj, attr, "") or "")), anchor)
        field.textChanged.connect(lambda text, o=obj, a=attr: (setattr(o, a, text), self._changed()))
        form.addRow(label, field)
        return field

    def _text(self, layout, value: str, setter, anchor: str, height: int = 82):
        editor = self._register(QTextEdit(), anchor)
        editor.setPlainText(value or "")
        editor.setMinimumHeight(height)
        editor.setMaximumHeight(max(height, 120))
        editor.textChanged.connect(lambda e=editor: (setter(e.toPlainText()), self._changed()))
        layout.addWidget(editor)
        return editor

    def _build_personal(self, outer):
        box, lay = self._section_box("Personal Info", "personal")
        form = QFormLayout()
        lay.addLayout(form)
        p = self.document.personal_info
        for attr, label in (("full_name", "Full name"), ("email", "Email"), ("phone", "Phone"), ("location", "Location"), ("linkedin", "LinkedIn"), ("github", "GitHub"), ("website", "Website")):
            self._line(form, label, p, attr, "personal")
        outer.addWidget(box)

    def _build_sections(self, outer):
        box, lay = self._section_box("Sections & order", "sections")
        lay.addWidget(QLabel("Drag-free controls for V1: select a section, then move it up/down. Uncheck to hide it from CareerOS-layout output."))
        order_list = QListWidget(); order_list.setMaximumHeight(230); lay.addWidget(order_list)
        labels = {"experience":"Experience", "education":"Education", "skills":"Skills", "languages":"Languages", "projects":"Projects", "certifications":"Certifications"}
        custom_keys = {f"custom:{section.id}": section.title or "Custom Section" for section in self.document.custom_sections}
        labels.update(custom_keys)
        order = [key for key in self.document.section_order if key in labels]
        for key in labels:
            if key not in order: order.append(key)
        hidden = set(self.layout_model.hidden_sections or [])
        order_list.blockSignals(True)
        for key in order:
            item = QListWidgetItem(labels[key]); item.setData(Qt.UserRole, key); item.setFlags(item.flags() | Qt.ItemIsUserCheckable); item.setCheckState(Qt.Unchecked if key in hidden else Qt.Checked); order_list.addItem(item)
        order_list.blockSignals(False)
        controls=QHBoxLayout(); up=QPushButton("Move Up"); down=QPushButton("Move Down"); controls.addWidget(up); controls.addWidget(down); controls.addStretch(); lay.addLayout(controls)
        def sync():
            self.document.section_order=[order_list.item(i).data(Qt.UserRole) for i in range(order_list.count())]
            self.layout_model.hidden_sections=[order_list.item(i).data(Qt.UserRole) for i in range(order_list.count()) if order_list.item(i).checkState()!=Qt.Checked]
            if "summary" not in self.layout_model.hidden_sections:
                self.layout_model.hidden_sections.append("summary")
            self._changed(True)
        def move(delta):
            row=order_list.currentRow(); target=row+delta
            if 0 <= row < order_list.count() and 0 <= target < order_list.count():
                item=order_list.takeItem(row); order_list.insertItem(target,item); order_list.setCurrentRow(target); sync()
        order_list.itemChanged.connect(lambda *_: sync()); up.clicked.connect(lambda: move(-1)); down.clicked.connect(lambda: move(1)); outer.addWidget(box)

    def _entity_buttons(self, layout, collection: list, item, anchor: str):
        row = QHBoxLayout()
        row.addStretch()
        up, down, delete = QPushButton("↑"), QPushButton("↓"), QPushButton("Delete")
        row.addWidget(up); row.addWidget(down); row.addWidget(delete)
        layout.addLayout(row)
        up.clicked.connect(lambda: self._move_entity(collection, item, -1, anchor))
        down.clicked.connect(lambda: self._move_entity(collection, item, 1, anchor))
        delete.clicked.connect(lambda: self._delete_entity(collection, item))

    def _bullet_editor(self, parent_layout, owner, anchor: str):
        parent_layout.addWidget(QLabel("Bullets"))
        for bullet in list(owner.bullets):
            row = QHBoxLayout()
            edit = self._register(QTextEdit(), bullet.id or anchor)
            edit.setPlainText(bullet.text)
            edit.setMaximumHeight(72)
            row.addWidget(edit, 1)
            up, down, delete = QPushButton("↑"), QPushButton("↓"), QPushButton("×")
            row.addWidget(up); row.addWidget(down); row.addWidget(delete)
            parent_layout.addLayout(row)
            edit.textChanged.connect(lambda b=bullet, e=edit: (setattr(b, "text", e.toPlainText().strip()), self._changed()))
            up.clicked.connect(lambda _=False, b=bullet: self._move_entity(owner.bullets, b, -1, anchor))
            down.clicked.connect(lambda _=False, b=bullet: self._move_entity(owner.bullets, b, 1, anchor))
            delete.clicked.connect(lambda _=False, b=bullet: self._delete_entity(owner.bullets, b))
        add = QPushButton("+ Add bullet")
        add.clicked.connect(lambda: (owner.bullets.append(ResumeBullet(id=self._new_id("bullet"), text="")), self._rebuild_editor(), self._changed(True), self._focus_anchor(anchor)))
        parent_layout.addWidget(add)

    def _build_education(self, outer):
        box, lay = self._section_box("Education", "education")
        for item in self.document.education:
            card = QGroupBox(item.institution or item.degree or "Education item")
            form = QFormLayout(card)
            for attr, label in (("institution", "Institution"), ("degree", "Degree"), ("field", "Field"), ("concentration", "Concentration"), ("location", "Location"), ("date_text", "Date")):
                self._line(form, label, item, attr, item.id)
            buttons = QHBoxLayout(); buttons.addStretch()
            up, down, delete = QPushButton("↑"), QPushButton("↓"), QPushButton("Delete")
            buttons.addWidget(up); buttons.addWidget(down); buttons.addWidget(delete); form.addRow(buttons)
            lay.addWidget(card); self._anchor_widgets[item.id] = card
            up.clicked.connect(lambda _=False, i=item: self._move_entity(self.document.education, i, -1, i.id))
            down.clicked.connect(lambda _=False, i=item: self._move_entity(self.document.education, i, 1, i.id))
            delete.clicked.connect(lambda _=False, i=item: self._delete_entity(self.document.education, i))
        add = QPushButton("+ Add education")
        add.clicked.connect(lambda: (self.document.education.append(EducationItem(id=self._new_id("education"))), self._rebuild_editor(), self._changed(True)))
        lay.addWidget(add); outer.addWidget(box)

    def _build_experience(self, outer):
        box, lay = self._section_box("Experience", "experience")
        for item in self.document.experience:
            card = QGroupBox(item.company or item.title or "Experience item")
            inner = QVBoxLayout(card); form = QFormLayout(); inner.addLayout(form)
            for attr, label in (("company", "Company"), ("title", "Title"), ("location", "Location"), ("date_text", "Date")):
                self._line(form, label, item, attr, item.id)
            self._bullet_editor(inner, item, item.id)
            self._entity_buttons(inner, self.document.experience, item, item.id)
            lay.addWidget(card); self._anchor_widgets[item.id] = card
        add = QPushButton("+ Add experience")
        add.clicked.connect(lambda: (self.document.experience.append(ExperienceItem(id=self._new_id("experience"))), self._rebuild_editor(), self._changed(True)))
        lay.addWidget(add); outer.addWidget(box)

    def _build_projects(self, outer):
        box, lay = self._section_box("Projects", "projects")
        for item in self.document.projects:
            card = QGroupBox(item.name or "Project")
            inner = QVBoxLayout(card); form = QFormLayout(); inner.addLayout(form)
            for attr, label in (("name", "Project name"), ("subtitle", "Subtitle"), ("organization", "Organization"), ("role", "Role"), ("location", "Location"), ("date_text", "Date"), ("url", "URL")):
                self._line(form, label, item, attr, item.id)
            self._bullet_editor(inner, item, item.id)
            self._entity_buttons(inner, self.document.projects, item, item.id)
            lay.addWidget(card); self._anchor_widgets[item.id] = card
        add = QPushButton("+ Add project")
        add.clicked.connect(lambda: (self.document.projects.append(ProjectItem(id=self._new_id("project"))), self._rebuild_editor(), self._changed(True)))
        lay.addWidget(add); outer.addWidget(box)

    def _build_skills(self, outer):
        box, lay = self._section_box("Skills", "skills")
        for group in self.document.skills:
            card = QGroupBox(group.name or "Skill group")
            inner = QVBoxLayout(card); form = QFormLayout(); inner.addLayout(form)
            self._line(form, "Group name", group, "name", group.id)
            items = self._register(QTextEdit(), group.id)
            items.setPlainText("\n".join(group.items))
            items.setPlaceholderText("One skill per line")
            items.setMaximumHeight(110)
            items.textChanged.connect(lambda g=group, e=items: (setattr(g, "items", [x.strip() for x in e.toPlainText().splitlines() if x.strip()]), setattr(g, "raw_text", ""), self._changed()))
            inner.addWidget(QLabel("Skills — one per line")); inner.addWidget(items)
            self._entity_buttons(inner, self.document.skills, group, group.id)
            lay.addWidget(card); self._anchor_widgets[group.id] = card
        add = QPushButton("+ Add skill group")
        add.clicked.connect(lambda: (self.document.skills.append(SkillGroup(id=self._new_id("skills"))), self._rebuild_editor(), self._changed(True)))
        lay.addWidget(add); outer.addWidget(box)

    def _build_languages(self, outer):
        box, lay = self._section_box("Languages", "languages")
        for item in self.document.languages:
            card = QGroupBox(item.language or "Language")
            form = QFormLayout(card)
            self._line(form, "Language", item, "language", item.id)
            self._line(form, "Proficiency", item, "proficiency", item.id)
            delete = QPushButton("Delete"); delete.clicked.connect(lambda _=False, i=item: self._delete_entity(self.document.languages, i)); form.addRow(delete)
            lay.addWidget(card); self._anchor_widgets[item.id] = card
        add = QPushButton("+ Add language")
        add.clicked.connect(lambda: (self.document.languages.append(LanguageItem(id=self._new_id("language"))), self._rebuild_editor(), self._changed(True)))
        lay.addWidget(add); outer.addWidget(box)

    def _build_certifications(self, outer):
        box, lay = self._section_box("Certifications", "certifications")
        edit = self._register(QTextEdit(), "certifications")
        edit.setPlainText("\n".join(self.document.certifications))
        edit.setMaximumHeight(100)
        edit.setPlaceholderText("One certification per line")
        edit.textChanged.connect(lambda: (setattr(self.document, "certifications", [x.strip() for x in edit.toPlainText().splitlines() if x.strip()]), self._changed()))
        lay.addWidget(edit); outer.addWidget(box)

    def _build_custom(self, outer):
        box, lay = self._section_box("Custom Sections", "custom")
        for section in self.document.custom_sections:
            card = QGroupBox(section.title or "Custom section")
            inner = QVBoxLayout(card); form = QFormLayout(); inner.addLayout(form)
            self._line(form, "Title", section, "title", section.id)
            lines = self._register(QTextEdit(), section.id)
            lines.setPlainText("\n".join(section.raw_lines))
            lines.setMaximumHeight(150)
            lines.textChanged.connect(lambda s=section, e=lines: (setattr(s, "raw_lines", e.toPlainText().splitlines()), self._changed()))
            inner.addWidget(QLabel("Content")); inner.addWidget(lines)
            delete = QPushButton("Delete"); delete.clicked.connect(lambda _=False, s=section: self._delete_custom(s)); inner.addWidget(delete)
            lay.addWidget(card); self._anchor_widgets[section.id] = card
        add = QPushButton("+ Add custom section"); add.clicked.connect(self._add_custom); lay.addWidget(add); outer.addWidget(box)

    def _move_entity(self, collection, item, delta: int, anchor: str):
        i = collection.index(item); target = i + delta
        if 0 <= target < len(collection):
            collection[i], collection[target] = collection[target], collection[i]
            self._rebuild_editor(); self._changed(True); self._focus_anchor(anchor)

    def _delete_entity(self, collection, item):
        collection.remove(item); self._rebuild_editor(); self._changed(True)

    def _add_custom(self):
        section = ResumeSection(id=self._new_id("section"), section_type="custom", title="New Section", raw_lines=[], visible=True)
        self.document.custom_sections.append(section)
        self.document.section_order.append(f"custom:{section.id}")
        self._rebuild_editor(); self._changed(True); self._focus_anchor(section.id)

    def _delete_custom(self, section):
        self.document.custom_sections.remove(section)
        self.document.section_order = [x for x in self.document.section_order if x != f"custom:{section.id}"]
        self._rebuild_editor(); self._changed(True)

    def _focus_anchor(self, anchor: str):
        widget = self._anchor_widgets.get(anchor)
        if widget is not None:
            self.editor_scroll.ensureWidgetVisible(widget, 0, 80)
            widget.setFocus(Qt.OtherFocusReason)
        self.preview.scrollToAnchor(anchor)

    def _preview_anchor_clicked(self, url: QUrl):
        if url.scheme() != "resume":
            return
        anchor = url.path().lstrip("/") or url.host()
        self._focus_anchor(anchor)

    @staticmethod
    def _a(anchor: str, label: str) -> str:
        return f'<a name="{html.escape(anchor)}"></a><a href="resume:///{html.escape(anchor)}" class="editlink">{label}</a>'

    def _preview_section(self, title: str, anchor: str, body: str) -> str:
        if not body.strip():
            return ""
        return f'<section><h2>{self._a(anchor, html.escape(title))}</h2>{body}</section>'

    def _build_preview_html(self) -> str:
        d = self.document; p = d.personal_info
        contacts = [p.email, p.phone, p.location, p.linkedin, p.github, p.website]
        contacts = [html.escape(x) for x in contacts if str(x).strip()]
        header = f'<header><h1>{self._a("personal", html.escape(p.full_name or "Your Name"))}</h1><div class="contacts">{" &nbsp;|&nbsp; ".join(contacts)}</div></header>'
        blocks = {}
        edu = []
        for i in d.education:
            left = " | ".join(x for x in [i.degree, i.institution] if x); sub = " · ".join(x for x in [i.concentration, i.location] if x)
            edu.append(f'<div class="entity"><table width="100%" cellspacing="0"><tr><td><b>{self._a(i.id, html.escape(left or "Education"))}</b></td><td align="right"><b>{html.escape(i.date_text)}</b></td></tr></table><div>{html.escape(sub)}</div></div>')
        blocks["education"] = self._preview_section("EDUCATION", "education", "".join(edu))
        exp = []
        for i in d.experience:
            title = " | ".join(x for x in [i.title, i.company] if x)
            bullets = "".join(f'<li>{self._a(b.id or i.id, html.escape(b.text))}</li>' for b in i.bullets if b.text)
            exp.append(f'<div class="entity"><table width="100%" cellspacing="0"><tr><td><b>{self._a(i.id, html.escape(title or "Experience"))}</b></td><td align="right"><b>{html.escape(i.date_text)}</b></td></tr></table><div>{html.escape(i.location)}</div><ul>{bullets}</ul></div>')
        blocks["experience"] = self._preview_section("EXPERIENCE", "experience", "".join(exp))
        skills = []
        for g in d.skills:
            skills.append(f'<div><b>{self._a(g.id, html.escape(g.name or "Skills"))}:</b> {html.escape(", ".join(g.items))}</div>')
        blocks["skills"] = self._preview_section("TECHNICAL SKILLS", "skills", "".join(skills))
        proj = []
        for i in d.projects:
            title = " | ".join(x for x in [i.name, i.subtitle] if x)
            bullets = "".join(f'<li>{self._a(b.id or i.id, html.escape(b.text))}</li>' for b in i.bullets if b.text)
            proj.append(f'<div class="entity"><table width="100%" cellspacing="0"><tr><td><b>{self._a(i.id, html.escape(title or "Project"))}</b></td><td align="right"><b>{html.escape(i.date_text)}</b></td></tr></table><ul>{bullets}</ul></div>')
        blocks["projects"] = self._preview_section("ENGINEERING PROJECTS", "projects", "".join(proj))
        langs = " · ".join(self._a(i.id, html.escape(" — ".join(x for x in [i.language, i.proficiency] if x))) for i in d.languages if i.language)
        blocks["languages"] = self._preview_section("LANGUAGES", "languages", f'<p>{langs}</p>' if langs else "")
        cert = "".join(f'<li>{html.escape(x)}</li>' for x in d.certifications)
        blocks["certifications"] = self._preview_section("CERTIFICATIONS", "certifications", f'<ul>{cert}</ul>' if cert else "")
        custom = {f"custom:{s.id}": self._preview_section(s.title or "ADDITIONAL INFORMATION", s.id, "".join(f'<div>{html.escape(line)}</div>' for line in s.raw_lines)) for s in d.custom_sections if s.visible}
        blocks.update(custom)
        order = list(d.section_order)
        for key in ("experience", "education", "skills", "languages", "projects", "certifications"):
            if key not in order:
                order.append(key)
        for key in custom:
            if key not in order:
                order.append(key)
        hidden = set(self.layout_model.hidden_sections or [])
        body = "".join(blocks.get(key, "") for key in order if key not in hidden)
        base = max(8.0, float(self.style.body_font_size or 9.0)); section = max(base + 1, float(self.style.section_font_size or base + 1)); name = max(section + 5, float(self.style.name_font_size or section + 5)); accent = html.escape(self.style.accent_color or "#1F4E79")
        return f"""<!doctype html><html><head><style>
        body{{font-family:Arial,Helvetica,sans-serif;background:#fff;color:#172033;font-size:{base}pt;line-height:{max(.95,float(self.style.line_spacing or 1.0))};margin:28px 34px}}
        header{{text-align:center;margin-bottom:10px}} h1{{font-size:{name}pt;color:{accent};margin:0 0 2px}} .contacts{{font-size:{max(7.5,base-1)}pt}}
        section{{margin:8px 0 0}} h2{{font-size:{section}pt;color:{accent};border-bottom:1.4px solid {accent};margin:0 0 4px;padding:0 0 2px}}
        .entity{{margin:4px 0}} table{{margin:0;padding:0}} td{{padding:0}} ul{{margin:2px 0 3px 18px;padding:0}} li{{margin:1px 0}} p{{margin:2px 0}}
        a.editlink{{color:inherit;text-decoration:none}} a.editlink:hover{{background:#e8f1ff}}
        </style></head><body>{header}{body}</body></html>"""

    def _normalize_section_order(self):
        """Keep newly-populated sections in a predictable professional order."""
        present = {
            "experience": bool(self.document.experience),
            "education": bool(self.document.education),
            "skills": bool(self.document.skills),
            "languages": bool(self.document.languages),
            "projects": bool(self.document.projects),
            "certifications": bool(self.document.certifications),
        }
        existing = [key for key in self.document.section_order if key.startswith("custom:") or key in present]
        # Newly added Experience needs a sensible home even when the imported
        # source never contained that heading.  Summary is intentionally hidden
        # from the CareerOS editor/output by default.
        existing = [key for key in existing if key != "summary"]
        if present["experience"] and "experience" not in existing:
            existing.insert(0, "experience")
        for key in ("education", "skills", "languages", "projects", "certifications"):
            if present[key] and key not in existing:
                existing.append(key)
        self.document.section_order = existing

    def _adjust_pdf_zoom(self, multiplier: float):
        if self.preview.zoomMode() != QPdfView.ZoomMode.Custom:
            self.preview.setZoomMode(QPdfView.ZoomMode.Custom)
            self.preview.setZoomFactor(1.0)
        self.preview.setZoomFactor(max(.35, min(3.0, self.preview.zoomFactor() * multiplier)))

    def refresh_preview(self):
        self._normalize_section_order()
        if self._preview_worker and self._preview_worker.isRunning():
            self._preview_pending = True
            self.preview_status.setText("Preview update queued…")
            return
        self._preview_generation += 1
        generation = self._preview_generation
        document = copy.deepcopy(self.document)
        style = copy.deepcopy(self.style)
        layout = copy.deepcopy(self.layout_model)
        self.preview_status.setText("Rendering exact PDF preview…")
        worker = TaskThread(self.service.render_editor_preview, document, style, layout, self.version_id, generation, self.document_type)
        self._preview_worker = worker

        def completed(path):
            if generation != self._preview_generation:
                return
            old = self._preview_path
            self.preview_pdf.close()
            self.preview_pdf.load(str(path))
            self.preview.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            self._preview_path = Path(path)
            self.preview_status.setText("Exact PDF preview — updates after you pause typing")
            if old and old != self._preview_path:
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    pass

        def finished():
            self._preview_worker = None
            if self._preview_pending:
                self._preview_pending = False
                self._preview_timer.start(80)

        worker.completed.connect(completed)
        worker.failed.connect(lambda message: self.preview_status.setText("Preview unavailable — Save still uses the document renderer"))
        worker.finished.connect(finished)
        worker.start()

    def save_changes(self) -> bool:
        self._record_snapshot()
        try:
            self._normalize_section_order()
            ResumeIntegrityValidator().validate(self.document)
            # The Resume page may currently have this exact PDF open.  On
            # Windows QPdfDocument keeps the file locked, which used to make the
            # Word export fail and silently fall back to the legacy ReportLab
            # renderer (destroying the professional layout).
            self.about_to_save.emit(self.version_id)
            QApplication.processEvents()
            self.service.update_version_document(self.version_id, self.document, self.style, self.layout_model)
            updated = next(v for v in self.service.db.resume_versions() if int(v["id"]) == self.version_id)
            self.service.render_version_outputs(updated)
            self._initial_json = self._snapshot_json(); self._history = [self._initial_json]; self._history_index = 0
            self._update_action_state(); self.saved.emit(self.version_id); self.preview_status.setText("Saved — generated DOCX/PDF refreshed")
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Save Resume", str(exc))
            return False

    def closeEvent(self, event):
        self._record_snapshot()
        if self._snapshot_json() == self._initial_json:
            event.accept(); return
        answer = QMessageBox.question(self, "Unsaved Changes", "Save changes before closing the Resume Editor?", QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if answer == QMessageBox.Save:
            event.accept() if self.save_changes() else event.ignore()
        elif answer == QMessageBox.Discard:
            event.accept()
        else:
            event.ignore()


class ResumePage(QWidget):
    def __init__(self, db: Database, resumes: ResumeService):
        super().__init__(); self.db, self.service, self.current_version, self.worker = db, resumes, None, None
        self.original_content = ""
        self._original_preview_path: Path | None = None
        self._editor_windows: list[ResumeEditorWindow] = []
        layout = QVBoxLayout(self); layout.setContentsMargins(22, 20, 22, 22); layout.setSpacing(10); layout.addWidget(QLabel("<h1>Resume & CV</h1><p style='color:#6e6e73;margin-top:-7px'>Review tailored Resume drafts and job-specific CV letters before approval.</p>")); buttons = QHBoxLayout(); self.open_file_button = QPushButton("Open Selected File…"); self.open_file_button.clicked.connect(self.open_selected_file_menu); self.edit_button = QPushButton("Edit Resume"); self.edit_button.clicked.connect(self.edit_selected_resume); self.approve_button = QPushButton("Approve Version"); self.approve_button.clicked.connect(lambda: self.decide(True)); self.reject_button = QPushButton("Reject Version"); self.reject_button.clicked.connect(lambda: self.decide(False)); self.delete_button = QPushButton("Delete Selected Draft"); self.delete_button.clicked.connect(self.delete_selected_draft)
        self.preview_zoom = QComboBox(); self.preview_zoom.addItems(["65%", "80%", "100%", "Fit width"]); self.preview_zoom.setCurrentText("80%")
        for w in (self.open_file_button, self.edit_button, self.approve_button, self.reject_button): w.setMaximumWidth(220); buttons.addWidget(w)
        buttons.addWidget(self.delete_button)
        buttons.addWidget(QLabel("Preview")); buttons.addWidget(self.preview_zoom)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.workspace_split = QSplitter(Qt.Horizontal); self.workspace_split.setChildrenCollapsible(False)
        info_panel = QWidget(); info_panel.setObjectName("resumeInfoPanel"); info_layout = QVBoxLayout(info_panel); info_layout.setContentsMargins(0, 0, 7, 0); info_layout.setSpacing(9)
        preview_panel = QWidget(); preview_layout = QVBoxLayout(preview_panel); preview_layout.setContentsMargins(7, 0, 0, 0); preview_layout.setSpacing(0)
        self.workspace_split.addWidget(info_panel); self.workspace_split.addWidget(preview_panel); self.workspace_split.setSizes([440, 960]); self.workspace_split.setStretchFactor(0, 0); self.workspace_split.setStretchFactor(1, 1)
        self.resume_setup_toggle = QPushButton("Resume setup  ▸"); self.resume_setup_toggle.setCheckable(True); self.resume_setup_toggle.toggled.connect(lambda open_: self._toggle_section(self.resume_setup, self.resume_setup_toggle, "Resume setup", open_)); info_layout.addWidget(self.resume_setup_toggle)
        self.resume_setup = QWidget(); self.resume_setup.setObjectName("settingsCard"); setup_form = QFormLayout(self.resume_setup); setup_form.setContentsMargins(16, 12, 16, 12); setup_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow); setup_form.setVerticalSpacing(7)
        setup_form.addRow(QLabel("<b>Active resume and document style</b><br/><span style='color:#6e6e73;font-size:11px'>Used for locally generated Resume and CV drafts.</span>"))
        resume_row = QVBoxLayout(); self.resume_label = QLabel(); self.resume_label.setWordWrap(True); self.resume_file_button = QPushButton("Select Resume File..."); self.resume_file_button.clicked.connect(self.import_resume); resume_row.addWidget(self.resume_file_button); resume_row.addWidget(self.resume_label); setup_form.addRow("Active resume", resume_row)
        pdf_options = load_settings().get("resume_pdf", {}); self.resume_pdf_style = QComboBox(); self.resume_pdf_style.addItems(["Modern", "Classic", "Compact"]); self.resume_pdf_style.setCurrentText(str(pdf_options.get("style", "Modern")))
        self.resume_font_family = QComboBox(); self.resume_font_family.addItems(["Helvetica", "Times"]); self.resume_font_family.setCurrentText(str(pdf_options.get("font_family", "Helvetica")))
        self.resume_pdf_font_size = QSpinBox(); self.resume_pdf_font_size.setRange(8, 12); self.resume_pdf_font_size.setSuffix(" pt"); self.resume_pdf_font_size.setValue(int(pdf_options.get("font_size", 9)))
        self.resume_line_spacing = QComboBox(); self.resume_line_spacing.addItems(["0.9", "1.0", "1.1", "1.2", "1.3", "1.4"]); self.resume_line_spacing.setCurrentText(str(pdf_options.get("line_spacing", 1.0)))
        self.resume_section_spacing = QComboBox(); self.resume_section_spacing.addItems(["Compact", "Normal", "Spacious"]); self.resume_section_spacing.setCurrentText(str(pdf_options.get("section_spacing", "Normal")))
        self.resume_pdf_margins = QComboBox(); self.resume_pdf_margins.addItems(["Narrow", "Standard", "Comfortable"]); self.resume_pdf_margins.setCurrentText(str(pdf_options.get("margins", "Standard")))
        self.resume_docx_layout = QComboBox(); self.resume_docx_layout.addItem("Professional one-page")
        setup_form.addRow("PDF style", self.resume_pdf_style); setup_form.addRow("Font family", self.resume_font_family); setup_form.addRow("Body font size", self.resume_pdf_font_size); setup_form.addRow("Line spacing", self.resume_line_spacing); setup_form.addRow("Section spacing", self.resume_section_spacing); setup_form.addRow("Page margins", self.resume_pdf_margins); setup_form.addRow("Word layout", self.resume_docx_layout); self.layout_hint = QLabel(); self.layout_hint.setWordWrap(True); self.layout_hint.setStyleSheet("color:#6e7786;font-size:11px"); setup_form.addRow("", self.layout_hint)
        self.apply_layout_button = QPushButton("Apply layout to selected draft")
        self.apply_layout_button.setToolTip("Re-render the selected Resume/CV with these layout settings. No AI call is made and the resume wording is not changed.")
        self.apply_layout_button.clicked.connect(self.apply_resume_options_to_selected)
        setup_form.addRow("", self.apply_layout_button)
        self.resume_pdf_controls = (self.resume_pdf_style, self.resume_font_family, self.resume_pdf_font_size, self.resume_line_spacing, self.resume_section_spacing, self.resume_pdf_margins); self.resume_docx_layout.currentTextChanged.connect(self.update_pdf_style_controls); self.update_pdf_style_controls()
        setup_form.addRow(QLabel("<b>Generation instructions</b><br/><span style='color:#6e6e73;font-size:11px'>Optional local guidance shared by Resume and CV generation. It supplements verification and safety rules.</span>"))
        prompts = load_settings().get("generation_prompts", {})
        self.resume_prompt = QTextEdit()
        self.resume_prompt.setPlaceholderText("Example: Emphasize CAD, CFD, and aerospace design experience. Use concise technical language.")
        self.resume_prompt.setPlainText(str(prompts.get("general", prompts.get("resume", ""))))
        self.resume_prompt.setFixedHeight(72)
        self.prompt_status = QLabel("Used for both Resume and CV. Saves automatically after you stop typing.")
        self.prompt_status.setStyleSheet("color:#6e6e73")
        setup_form.addRow("Resume / CV Instructions", self.resume_prompt)
        setup_form.addRow("Status", self.prompt_status)
        info_layout.addWidget(self.resume_setup); self.resume_setup.hide(); self.refresh_resume_label()
        # Diagnostics remain internal and lazy-loaded only for support tooling.
        self.debug_toggle = QPushButton(); self.debug_toggle.hide()
        self.debug_panel = QWidget(); self.debug_panel.setObjectName("settingsCard"); debug_layout = QVBoxLayout(self.debug_panel); debug_layout.setContentsMargins(16, 12, 16, 12); debug_layout.setSpacing(7)
        debug_layout.addWidget(QLabel("<b>Read-only structured resume diagnostics</b><br/><span style='color:#6e6e73;font-size:11px'>Shows only local parsing, IDs, section ownership and integrity checks. It is never sent to AI.</span>"))
        self.debug_summary = QLabel(); self.debug_summary.setWordWrap(True); self.debug_summary.setStyleSheet("color:#334155;font-size:11px")
        self.debug_text = QTextEdit(); self.debug_text.setReadOnly(True); self.debug_text.setMinimumHeight(220); self.debug_text.setLineWrapMode(QTextEdit.NoWrap); self.debug_text.setFont(QFont("Consolas", 9))
        copy_debug = QPushButton("Copy debug JSON"); copy_debug.clicked.connect(lambda: QApplication.clipboard().setText(self.debug_text.toPlainText()))
        debug_layout.addWidget(self.debug_summary); debug_layout.addWidget(self.debug_text); debug_layout.addWidget(copy_debug); self.debug_panel.hide()
        self.resume_options_timer = QTimer(self); self.resume_options_timer.setSingleShot(True); self.resume_options_timer.timeout.connect(self.save_resume_options)
        for control in (self.resume_pdf_style, self.resume_font_family, self.resume_pdf_font_size, self.resume_line_spacing, self.resume_section_spacing, self.resume_pdf_margins, self.resume_docx_layout):
            (getattr(control, "currentTextChanged", None) or getattr(control, "valueChanged")).connect(self.schedule_resume_options_save)
        self.prompt_timer = QTimer(self); self.prompt_timer.setSingleShot(True); self.prompt_timer.timeout.connect(self.save_generation_prompts); self.resume_prompt.textChanged.connect(self.schedule_prompt_save)
        self.interested_box = QWidget(); self.interested_box.setObjectName("settingsCard"); interested_layout = QVBoxLayout(self.interested_box); interested_layout.setContentsMargins(16, 12, 16, 12); interested_layout.setSpacing(7)
        interested_layout.addWidget(QLabel("<b>Interested jobs</b><br/><span style='color:#6e6e73;font-size:11px'>Create a tailored Resume or a letter-style CV using the Resume, verified Settings information, and the selected job.</span>"))
        self.interested_jobs = QListWidget(); self.interested_jobs.setObjectName("versionList"); self.interested_jobs.setMinimumHeight(115); interested_layout.addWidget(self.interested_jobs)
        info_layout.addWidget(self.interested_box, 1)
        self.versions = QListWidget(); self.versions.setObjectName("versionList"); self.versions.currentRowChanged.connect(self.select_resume_version)
        self.cv_versions = QListWidget(); self.cv_versions.setObjectName("versionList"); self.cv_versions.currentRowChanged.connect(self.select_cv_version)
        self.document_lists = QTabWidget(); self.document_lists.setObjectName("documentLists"); self.document_lists.setMinimumHeight(130)
        self.document_lists.addTab(self.versions, "Resume Drafts")
        self.document_lists.addTab(self.cv_versions, "CV Drafts")
        self.generate_draft_button = QPushButton("Generate Resume Draft")
        self.generate_draft_button.clicked.connect(lambda: self.generate_interested("CV" if self.document_lists.currentIndex() == 1 else "Resume"))
        self.document_lists.setCornerWidget(self.generate_draft_button, Qt.TopRightCorner)
        info_layout.addWidget(self.document_lists, 1)
        self.document_stack = QStackedWidget(); self.resume_panel = QWidget(); resume_layout = QVBoxLayout(self.resume_panel); resume_layout.setContentsMargins(0, 0, 0, 0)
        self.resume_tabs = QTabWidget(); self.original_pdf, self.modified_pdf = QPdfDocument(self), QPdfDocument(self); self.original_view, self.modified_view = QPdfView(), QPdfView(); self.original_view.setDocument(self.original_pdf); self.modified_view.setDocument(self.modified_pdf)
        for view in (self.original_view, self.modified_view): view.setPageMode(QPdfView.PageMode.MultiPage)
        comparison = QSplitter(Qt.Horizontal); self.compare_old = self._comparison_text("Original resume", "#8f1d1d"); self.compare_new = self._comparison_text("Generated resume", "#146c3b"); comparison.addWidget(self.compare_old); comparison.addWidget(self.compare_new); comparison.setSizes([600, 600])
        self.resume_tabs.addTab(self.original_view, "Original Resume Preview"); self.resume_tabs.addTab(self.modified_view, "Generated Resume Preview"); self.resume_tabs.addTab(comparison, "Compare Changes")
        resume_layout.addWidget(self.resume_tabs); self.document_stack.addWidget(self.resume_panel)
        self.cover_panel = QWidget(); cover_layout = QVBoxLayout(self.cover_panel); cover_layout.setContentsMargins(0, 0, 0, 0); self.cover_heading = QLabel("<h3>CV / Cover Letter Preview</h3>"); cover_layout.addWidget(self.cover_heading); self.cover_pdf = QPdfDocument(self); self.cover_view = QPdfView(); self.cover_view.setDocument(self.cover_pdf); self.cover_view.setPageMode(QPdfView.PageMode.MultiPage); cover_layout.addWidget(self.cover_view); self.document_stack.addWidget(self.cover_panel)
        self.preview_zoom.currentTextChanged.connect(self.set_preview_zoom); self.set_preview_zoom(self.preview_zoom.currentText()); self.document_lists.currentChanged.connect(self.show_document_category); preview_layout.addWidget(self.document_stack, 1); layout.addWidget(self.workspace_split, 1); self.refresh(load_preview=False)

    @staticmethod
    def _toggle_section(section: QWidget, button: QPushButton, title: str, open_: bool) -> None:
        section.setVisible(open_); button.setText(f"{title}  {'▾' if open_ else '▸'}")

    def schedule_resume_options_save(self, *_):
        self.resume_options_timer.start(500)

    def save_resume_options(self):
        settings = load_settings(); settings["resume_pdf"] = {"style": self.resume_pdf_style.currentText(), "font_family": self.resume_font_family.currentText(), "font_size": self.resume_pdf_font_size.value(), "line_spacing": float(self.resume_line_spacing.currentText()), "section_spacing": self.resume_section_spacing.currentText(), "margins": self.resume_pdf_margins.currentText(), "docx_layout": self.resume_docx_layout.currentText()}; save_settings(settings)

    def apply_resume_options_to_selected(self):
        """Re-render the selected draft with the current layout settings only.

        This is intentionally separate from AI generation: wording/content stays
        exactly as stored in ResumeDocument while font, spacing, margins and
        Word-layout mode are updated.
        """
        self.save_resume_options()
        version = self.current_version
        if not version:
            QMessageBox.information(self, "Apply layout", "Select a Resume or CV draft first.")
            return
        if version.get("approved"):
            QMessageBox.information(self, "Apply layout", "Approved versions stay unchanged. Select or generate a review draft first.")
            return
        if str(version.get("document_type", "Resume")).upper() == "CV":
            try:
                self.cover_pdf.close()
                self.service.db.update_resume_version(int(version["id"]), style_json=json.dumps(resume_style_to_dict(self.service.default_style())), layout_json=json.dumps(resume_layout_to_dict(self.service.default_layout())))
                version = next(item for item in self.db.resume_versions() if int(item["id"]) == int(version["id"]))
                self.service.render_version_outputs(version)
                selected_id = int(version["id"]); self.refresh(load_preview=False)
                row = next((index for index, value in enumerate(self.cv_data) if int(value.get("id") or -1) == selected_id), -1)
                if row >= 0:
                    self.cv_versions.setCurrentRow(row); self.select_cv_version(row, load_preview=True)
                QMessageBox.information(self, "Apply layout", "CV letter layout updated without calling AI. Its wording was not changed.")
            except Exception as exc:
                log.exception("Could not apply CV letter layout without AI"); QMessageBox.critical(self, "Apply layout", str(exc))
            return
        try:
            # Release the currently displayed PDF before Word replaces it on
            # Windows. This is the same lock-avoidance used by the editor save.
            try:
                self.modified_pdf.close()
            except Exception:
                pass
            document = self.service.document_from_version(version)
            current_layout = self.service.layout_from_version(version)
            style = self.service.default_style()
            layout = self.service.default_layout()
            # Layout settings should not erase editor choices such as hidden
            # sections. Only page geometry / renderer mode comes from Settings.
            layout.hidden_sections = list(current_layout.hidden_sections or [])
            layout.section_order = list(current_layout.section_order or [])
            if "summary" not in layout.hidden_sections:
                layout.hidden_sections.append("summary")
            self.service.update_version_document(int(version["id"]), document, style, layout)
            refreshed = next((item for item in self.db.resume_versions() if int(item.get("id") or -1) == int(version["id"])), None)
            if not refreshed:
                raise RuntimeError("The selected draft could not be reloaded after updating its layout.")
            self.service.render_version_outputs(refreshed)
            selected_id = int(version["id"])
            self.refresh(load_preview=False)
            kind = str(refreshed.get("document_type", "Resume")).upper()
            collection = self.cv_data if kind == "CV" else self.resume_data
            widget = self.cv_versions if kind == "CV" else self.versions
            row = next((index for index, value in enumerate(collection) if int(value.get("id") or -1) == selected_id), -1)
            if row >= 0:
                widget.setCurrentRow(row)
                (self.select_cv_version if kind == "CV" else self.select_resume_version)(row, load_preview=True)
            QMessageBox.information(self, "Apply layout", "Layout updated without calling AI. Resume/CV wording was not changed.")
        except Exception as exc:
            log.exception("Could not apply resume layout without AI")
            QMessageBox.critical(self, "Apply layout", str(exc))

    def update_pdf_style_controls(self, *_):
        self.layout_hint.setText("CareerOS uses a structured Resume renderer and a separate professional letter renderer for CV drafts.")
        for control in self.resume_pdf_controls:
            control.setEnabled(True)
        note = ""
        for control in self.resume_pdf_controls:
            control.setToolTip(note)

    def import_resume(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose Original Resume", "", "Word Documents (*.docx)")
        if not path:
            return
        try:
            extracted = self.service.import_original(path); self.refresh_resume_label(); self.update_pdf_style_controls(); self.refresh()
            QMessageBox.information(self, "Resume", f"Imported safely. Local resume text extracted to:\n{extracted}")
        except Exception as exc:
            QMessageBox.critical(self, "Resume import", str(exc))

    def refresh_resume_label(self):
        source = self.service.imported_source_path()
        self.resume_label.setText(f"Selected: {source.name}\nStored safely in CareerOS." if source else "No resume selected yet. Choose a file above to begin.")

    def generate_interested(self, document_type: str):
        row = self.interested_jobs.currentRow(); job = self.interested_data[row] if 0 <= row < len(self.interested_data) else None
        if not job:
            QMessageBox.information(self, "Generate draft", "Mark a job as Interested in Jobs, then select it here.")
            return
        kind = "CV" if document_type.upper() == "CV" else "Resume"
        existing = next((version for version in self.data if version.get("job_id") == job["id"] and version.get("document_type", "Resume") == kind and not version.get("approved")), None)
        if existing and QMessageBox.question(self, "Regenerate draft", f"A {kind} draft already exists for this job. Regenerate and replace that local draft?") != QMessageBox.Yes:
            return
        settings = load_settings()
        if str(settings.get("ai_mode", "Auto")).startswith("API:"):
            if QMessageBox.question(self, "Send data to external AI?", f"This creates a local {document_type} draft only; no application is submitted. Continue?") != QMessageBox.Yes:
                return
        if existing:
            try:
                (self.cover_pdf if kind == "CV" else self.modified_pdf).close()
            except Exception:
                pass
        self.generate_draft_button.setEnabled(False)
        self.worker = TaskThread(self.service.optimize, job, None, kind, existing)
        shell = self.window()
        if hasattr(shell, "begin_task_state"):
            shell.begin_task_state(self.worker, f"Generating {document_type} draft…")
        def done(result):
            self.generate_draft_button.setEnabled(bool(self.interested_data))
            if hasattr(shell, "finish_task_state"): shell.finish_task_state("Draft regenerated for review." if existing else "Draft created for review.")
            self.current_version = None; self.refresh(load_preview=False)
            version_id = result.get("version_id") if isinstance(result, dict) else None
            if kind == "CV":
                self.document_lists.setCurrentIndex(1)
                row = next((index for index, value in enumerate(self.cv_data) if value.get("id") == version_id), -1)
                if row >= 0:
                    self.cv_versions.setCurrentRow(row)
            else:
                self.document_lists.setCurrentIndex(0)
                row = next((index for index, value in enumerate(self.resume_data) if value.get("id") == version_id), -1)
                if row >= 0:
                    self.versions.setCurrentRow(row)
            warnings = result.get("generation_warnings", []) if isinstance(result, dict) else []
            if warnings:
                QMessageBox.information(self, f"{kind} created safely", "\n\n".join(str(item) for item in warnings))
        def failed(error):
            self.generate_draft_button.setEnabled(bool(self.interested_data))
            if hasattr(shell, "finish_task_state"): shell.finish_task_state("Generation failed.")
            # Full traceback is already written by TaskThread to the application log.
            # Keep the user-facing dialog concise and actionable.
            message = str(error).split("\n\nTraceback", 1)[0].strip()
            QMessageBox.critical(self, "Generate draft", message or "Resume generation failed. Your original resume was not changed.")
        self.worker.completed.connect(done); self.worker.failed.connect(failed)
        self.worker.finished.connect(lambda: self.generate_draft_button.setEnabled(bool(self.interested_data)))
        self.worker.start()

    def schedule_prompt_save(self):
        self.prompt_status.setText("Saving instructions..."); self.prompt_timer.start(600)

    def save_generation_prompts(self):
        settings = load_settings(); settings["generation_prompts"] = {"general": self.resume_prompt.toPlainText().strip()}; save_settings(settings); self.prompt_status.setText("Resume / CV instructions saved locally.")

    def set_preview_zoom(self, value: str):
        views = (self.original_view, self.modified_view, self.cover_view)
        if value == "Fit width":
            for view in views: view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            return
        factor = int(value.rstrip("%")) / 100
        for view in views: view.setZoomMode(QPdfView.ZoomMode.Custom); view.setZoomFactor(factor)

    def show_document_category(self, index: int):
        """Keep the preview in sync when the user switches Resume/CV tabs."""
        self.generate_draft_button.setText("Generate CV Draft" if index == 1 else "Generate Resume Draft")
        running = bool(self.worker and self.worker.isRunning())
        self.generate_draft_button.setEnabled(bool(getattr(self, "interested_data", [])) and not running)
        if index == 0:
            row = self.versions.currentRow()
            if 0 <= row < len(self.resume_data):
                self.select_resume_version(row)
            else:
                self.current_version = None; self.approve_button.setEnabled(False); self.reject_button.setEnabled(False); self.edit_button.setEnabled(False); self.document_stack.setCurrentWidget(self.resume_panel)
        else:
            row = self.cv_versions.currentRow()
            if 0 <= row < len(self.cv_data):
                self.select_cv_version(row)
            else:
                self.current_version = None; self.approve_button.setEnabled(False); self.reject_button.setEnabled(False); self.edit_button.setEnabled(False); self.document_stack.setCurrentWidget(self.cover_panel)

    @staticmethod
    def _comparison_text(title: str, color: str) -> QTextEdit:
        widget = QTextEdit(); widget.setReadOnly(True); widget.setStyleSheet(f"QTextEdit{{background:#ffffff;padding:14px;border:1px solid #d2d2d7;border-radius:9px;font-family:'Segoe UI';font-size:12px}} h3{{color:{color};margin:0 0 10px 0}} .same{{color:#4b5563}} .changed{{background:{'#fee2e2' if color == '#8f1d1d' else '#dcfce7'};color:{color};padding:2px 3px;border-radius:3px}}")
        widget.setHtml(f"<h3>{title}</h3><p style='color:#6e6e73'>Choose a resume draft to compare.</p>")
        return widget

    def refresh(self, load_preview: bool = True):
        # Preserve list identity across refreshes.  Previously every navigation
        # cleared current_version while leaving the old preview on
        # screen.  That made the first Delete click act as though nothing was
        # selected; the user had to select the same draft again.
        selected_version_id = self.current_version.get("id") if self.current_version else None
        current_item = self.versions.currentItem() if hasattr(self, "versions") else None
        cv_item = self.cv_versions.currentItem() if hasattr(self, "cv_versions") else None
        if selected_version_id is None and current_item is not None:
            selected_version_id = current_item.data(Qt.UserRole)
        if selected_version_id is None and cv_item is not None:
            selected_version_id = cv_item.data(Qt.UserRole)
        try:
            self.original_content = self.service.original_text()
            if load_preview:
                self._refresh_original_preview()
        except Exception:
            self.original_content = ""; self.compare_old.setHtml("<p style='color:#64748b'>Import an original resume in Settings to begin review.</p>")
        selected_interest = self.interested_jobs.currentItem().data(Qt.UserRole) if self.interested_jobs.currentItem() else None
        all_jobs = self.db.jobs() if hasattr(self.db, "jobs") else []
        self.interested_data = [job for job in all_jobs if job.get("status") == "Interested"]; self.interested_jobs.clear()
        for job in self.interested_data:
            item = QListWidgetItem(f"{job['company']} · {job['title']} · {job['location']}"); item.setData(Qt.UserRole, job["id"]); self.interested_jobs.addItem(item)
            if job["id"] == selected_interest: self.interested_jobs.setCurrentItem(item)
        if self.interested_data and self.interested_jobs.currentRow() < 0:
            self.interested_jobs.setCurrentRow(0)
        if not self.interested_data:
            empty = QListWidgetItem("No interested jobs yet. Mark a job as Interested in Jobs to prepare a Resume or CV."); empty.setFlags(Qt.NoItemFlags); self.interested_jobs.addItem(empty)
        self.generate_draft_button.setEnabled(bool(self.interested_data) and not bool(self.worker and self.worker.isRunning()))

        self.data = self.db.resume_versions()
        # Approved files move to Applications.  For a job/type, keep only the
        # newest review draft visible, so regenerating a CV never looks like two
        # separate active CV drafts.
        review = [v for v in self.data if not v.get("approved")]
        self.resume_data = [v for v in review if str(v.get("document_type", "Resume")).upper() != "CV"]
        seen_cv_jobs: set[object] = set()
        self.cv_data = []
        for version in review:
            if str(version.get("document_type", "Resume")).upper() != "CV":
                continue
            key = version.get("job_id") if version.get("job_id") is not None else version.get("id")
            if key in seen_cv_jobs:
                continue
            seen_cv_jobs.add(key); self.cv_data.append(version)
        self.versions.blockSignals(True); self.versions.clear()
        self.cv_versions.blockSignals(True); self.cv_versions.clear()
        restore_version_row = -1; restore_cv_row = -1
        for row, v in enumerate(self.resume_data):
            item = QListWidgetItem(f"Resume · {v['version_name']} · {v['company'] or ''} · {'Approved' if v['approved'] else 'Rejected' if v['rejected'] else 'Review'}")
            item.setData(Qt.UserRole, v["id"]); self.versions.addItem(item)
            if v["id"] == selected_version_id: restore_version_row = row
        for row, v in enumerate(self.cv_data):
            state = "Legacy — regenerate" if not self.service.is_cv_letter_version(v) else "Rejected" if v["rejected"] else "Review"
            item = QListWidgetItem(f"CV · {v['version_name']} · {v['company'] or ''} · {state}")
            item.setData(Qt.UserRole, v["id"]); self.cv_versions.addItem(item)
            if v["id"] == selected_version_id: restore_cv_row = row
        self.versions.blockSignals(False); self.cv_versions.blockSignals(False)

        if self.document_lists.currentIndex() == 0 and restore_version_row >= 0:
            self.versions.blockSignals(True); self.versions.setCurrentRow(restore_version_row); self.versions.blockSignals(False)
            self.select_resume_version(restore_version_row, load_preview=False)
        elif self.document_lists.currentIndex() == 1 and restore_cv_row >= 0:
            self.cv_versions.blockSignals(True); self.cv_versions.setCurrentRow(restore_cv_row); self.cv_versions.blockSignals(False)
            self.select_cv_version(restore_cv_row, load_preview=False)
        else:
            self.current_version = None
            self.approve_button.setEnabled(False); self.reject_button.setEnabled(False); self.edit_button.setEnabled(False); self.delete_button.setEnabled(False)

        # Structured diagnostics can reparse a DOCX when no sidecar exists.
        # Keep that work lazy because the panel is hidden during normal use.
        if self.debug_panel.isVisible():
            self.refresh_structured_debug()

    def load_prepared_original_preview(self, path: Path | None) -> None:
        if not path or not Path(path).is_file():
            return
        path = Path(path)
        if self._original_preview_path == path:
            return
        self._load_pdf(self.original_pdf, path)
        self._original_preview_path = path

    def _refresh_original_preview(self) -> None:
        path = self.service.prepare_original_preview()
        self.load_prepared_original_preview(path)

    def toggle_structured_debug(self, open_: bool) -> None:
        self._toggle_section(self.debug_panel, self.debug_toggle, "Structured debug", open_)
        if open_:
            self.refresh_structured_debug()

    def refresh_structured_debug(self) -> None:
        """Expose local model diagnostics without changing any stored resume."""
        try:
            version = self.current_version
            document = self.service.document_from_version(version) if version else self.service.original_document()
            layout = self.service.layout_from_version(version) if version else self.service.default_layout()
            source = f"Draft {version['version_name']}" if version else "Current imported resume"
            integrity = ResumeIntegrityValidator().report(document)
            payload = {
                "source": source,
                "integrity": integrity,
                "section_order": document.section_order,
                "hidden_sections": layout.hidden_sections,
                "counts": {"education": len(document.education), "experience": len(document.experience), "projects": len(document.projects), "skill_groups": len(document.skills), "custom_sections": len(document.custom_sections)},
                "resume": resume_document_to_dict(document),
            }
            self.debug_summary.setText(f"{source} · Integrity: {integrity['status']} · Education {len(document.education)}, Experience {len(document.experience)}, Projects {len(document.projects)}, Skills {len(document.skills)} · Issues {len(integrity['issues'])}")
            self.debug_text.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:
            self.debug_summary.setText("No resume diagnostics available.")
            self.debug_text.setPlainText(json.dumps({"integrity": {"status": "NOT AVAILABLE", "message": str(exc)}}, ensure_ascii=False, indent=2))

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

    def _compare_html(self, original: str, modified: str, document_label: str = "Resume") -> str:
        label = html.escape(document_label)
        old_rows = [f"<h3 style='color:#8f1d1d'>Original {label}</h3><p style='color:#6e6e73'>Red material was removed or replaced.</p>"]
        new_rows = [f"<h3 style='color:#146c3b'>Generated {label}</h3><p style='color:#6e6e73'>Green material is new or replaced.</p>"]
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

    def _select_version_record(self, version: dict, load_preview: bool = True):
        self.current_version = version; content = version["content"]
        is_cv = str(version.get("document_type", "Resume")).upper() == "CV"
        noun = "CV" if is_cv else "Resume"
        if is_cv:
            legacy = not self.service.is_cv_letter_version(version)
            self.edit_button.setText("Edit CV Letter")
            self.cover_heading.setText("<h3>Legacy CV — regenerate this draft</h3>" if legacy else "<h3>CV / Cover Letter Preview</h3>")
            try:
                self.modified_pdf.close()
            except Exception:
                pass
            self.approve_button.setEnabled(not legacy); self.reject_button.setEnabled(True); self.edit_button.setEnabled(not bool(version.get("approved"))); self.delete_button.setEnabled(not bool(version.get("approved")))
            if load_preview:
                generated = self.service.paths.resumes_generated / f"{version['version_name']}.pdf"
                if not generated.exists():
                    generated, _ = self.service.render_version_outputs(version)
                self._load_pdf(self.cover_pdf, generated)
            self.document_stack.setCurrentWidget(self.cover_panel)
            return
        self.resume_tabs.setTabText(0, f"Original {noun} Preview")
        self.resume_tabs.setTabText(1, f"Generated {noun} Preview")
        self.resume_tabs.setTabText(2, f"Compare {noun} Changes")
        self.edit_button.setText(f"Edit {noun}")
        # A stale cover preview must never win when the user selects a CV.
        try:
            self.cover_pdf.close()
        except Exception:
            pass
        self.approve_button.setEnabled(True); self.reject_button.setEnabled(True); self.edit_button.setEnabled(not bool(version.get("approved"))); self.delete_button.setEnabled(not bool(version.get("approved")))
        if load_preview:
            generated = self.service.paths.resumes_generated / f"{version['version_name']}.pdf"
            self._load_pdf(self.modified_pdf, generated if generated.exists() else self.service.preview_pdf(content, "modified-preview"))
            old_html, new_html = self._compare_html(self.original_content, content, noun); self.compare_old.setHtml(old_html); self.compare_new.setHtml(new_html)
        self.document_stack.setCurrentWidget(self.resume_panel); self.resume_tabs.setCurrentIndex(1)
        if self.debug_panel.isVisible():
            self.refresh_structured_debug()

    def select_resume_version(self, row, load_preview: bool = True):
        if row < 0 or row >= len(self.resume_data): return
        self._select_version_record(self.resume_data[row], load_preview)

    def select_cv_version(self, row, load_preview: bool = True):
        if row < 0 or row >= len(self.cv_data): return
        self._select_version_record(self.cv_data[row], load_preview)

    @staticmethod
    def _editor_section_text(document, key: str) -> str:
        if key == "experience": return "\n".join(line for item in document.experience for line in [*item.extra_lines, *("- " + bullet.text for bullet in item.bullets)])
        if key == "education": return "\n".join(line for item in document.education for line in [*item.details, *item.extra_lines])
        if key == "skills": return "\n".join(group.raw_text or f"{group.name}: {', '.join(group.items)}" for group in document.skills)
        if key == "projects": return "\n".join(line for item in document.projects for line in [*item.extra_lines, *("- " + bullet.text for bullet in item.bullets)])
        if key == "certifications": return "\n".join(document.certifications)
        if key == "languages": return "\n".join(item.language if hasattr(item, "language") else str(item) for item in document.languages)
        if key == "custom": return "\n\n".join("\n".join([section.title, *section.raw_lines]) for section in document.custom_sections)
        return ""

    @staticmethod
    def _apply_editor_section(document, key: str, text: str) -> None:
        text = text.strip()
        if key in {"experience", "education", "skills", "projects", "certifications", "languages"}:
            parsed = parse_resume_text(f"{key.upper()}\n{text}")
            setattr(document, key, getattr(parsed, key))
            return
        if key == "custom":
            lines = text.splitlines(); document.custom_sections = [ResumeSection("custom-editor", "custom", "Additional information", lines, True)] if lines else []
            document.section_order = [entry for entry in document.section_order if not entry.startswith("custom:")]
            if lines: document.section_order.append("custom:custom-editor")

    def edit_selected_resume(self):
        if not self.current_version:
            QMessageBox.information(self, "Edit Resume", "Select a review Resume or CV draft first.")
            return
        if self.current_version.get("approved"):
            QMessageBox.information(self, "Edit Resume", "Approved versions stay as records. Generate a new review draft to edit it.")
            return
        if str(self.current_version.get("document_type", "Resume")).upper() == "CV":
            self.edit_selected_cv_letter()
            return
        layout_model = self.service.layout_from_version(self.current_version)
        # Editor V1 is a real top-level, non-modal window. Keep a Python
        # reference so Qt does not collect it while the user works in another
        # CareerOS window or on a second monitor.
        window = ResumeEditorWindow(self.service, self.current_version)
        self._editor_windows.append(window)
        version_id = int(self.current_version["id"])

        def on_saved(saved_version_id: int):
            selected = saved_version_id or version_id
            self.refresh(load_preview=False)
            kind = str((self.current_version or {}).get("document_type", "Resume")).upper()
            collection = self.cv_data if kind == "CV" else self.resume_data
            widget = self.cv_versions if kind == "CV" else self.versions
            row = next((index for index, value in enumerate(collection) if int(value.get("id")) == int(selected)), -1)
            if row >= 0:
                widget.setCurrentRow(row)
                (self.select_cv_version if kind == "CV" else self.select_resume_version)(row, load_preview=True)

        def on_closed(*_):
            try:
                self._editor_windows.remove(window)
            except ValueError:
                pass

        def before_save(saved_version_id: int):
            # Release the generated PDF before Word overwrites it.  This is
            # essential on Windows; otherwise export can fail due to the file
            # lock and trigger the visually different fallback renderer.
            if self.current_version and int(self.current_version.get("id") or -1) == int(saved_version_id):
                try:
                    self.modified_pdf.close()
                except Exception:
                    pass

        window.about_to_save.connect(before_save)
        window.saved.connect(on_saved)
        window.destroyed.connect(on_closed)
        window.show()
        window.raise_()
        window.activateWindow()

    def edit_selected_cv_letter(self):
        version = self.current_version
        if not version:
            return
        dialog = QDialog(self); dialog.setWindowTitle(f"Edit CV Letter — {version['version_name']}"); dialog.resize(760, 680)
        layout = QVBoxLayout(dialog)
        note = QLabel("Edit the letter body below. CareerOS adds the candidate header, date, company, location, and job title to the Word/PDF output.")
        note.setWordWrap(True); layout.addWidget(note)
        editor = QTextEdit(); editor.setPlainText(version.get("content") or ""); layout.addWidget(editor, 1)
        actions = QHBoxLayout(); save = QPushButton("Save CV Letter"); cancel = QPushButton("Cancel"); actions.addStretch(); actions.addWidget(cancel); actions.addWidget(save); layout.addLayout(actions)
        cancel.clicked.connect(dialog.reject)

        def persist():
            text = editor.toPlainText().strip()
            if not text:
                QMessageBox.information(dialog, "CV Letter", "The CV letter cannot be empty.")
                return
            try:
                self.cover_pdf.close()
                self.service.update_cv_letter(int(version["id"]), text)
            except Exception as exc:
                QMessageBox.critical(dialog, "CV Letter", str(exc))
                return
            dialog.accept()

        save.clicked.connect(persist)
        if dialog.exec() == QDialog.Accepted:
            selected_id = int(version["id"]); self.refresh(load_preview=False)
            row = next((index for index, value in enumerate(self.cv_data) if int(value.get("id") or -1) == selected_id), -1)
            if row >= 0:
                self.document_lists.setCurrentIndex(1); self.cv_versions.setCurrentRow(row); self.select_cv_version(row, load_preview=True)

    def delete_selected_draft(self):
        is_cv = self.document_lists.currentIndex() == 1
        widget = self.cv_versions if is_cv else self.versions
        collection = self.cv_data if is_cv else self.resume_data
        item = widget.currentItem()
        version_id = item.data(Qt.UserRole) if item is not None else (self.current_version.get("id") if self.current_version else None)
        version = next((value for value in collection if value.get("id") == version_id), None)
        if not version:
            QMessageBox.information(self, "Delete Draft", "Select a draft first.")
            return
        if version.get("approved"):
            QMessageBox.information(self, "Delete Draft", "Approved versions are kept as records and cannot be deleted here.")
            return
        label = version["version_name"]
        if QMessageBox.question(self, "Delete Draft", f"Delete the local {label} draft and its generated PDF? This cannot be undone.") != QMessageBox.Yes:
            return
        record = self.db.remove_resume_version(int(version_id))
        self.current_version = None
        if record:
            # QPdfDocument can keep either preview locked on Windows.
            try:
                (self.cover_pdf if is_cv else self.modified_pdf).close()
            except Exception:
                pass
            for output in (
                self.service.paths.resumes_generated / f"{record['version_name']}.pdf",
                self.service.paths.resumes_generated / f"{record['version_name']}.docx",
            ):
                try:
                    output.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Could not remove deleted draft output %s: %s", output, exc)
        self.refresh(load_preview=False)
        # Keep the list visibly in sync after deletion. Select the first remaining
        # draft (if any) so the preview/action state cannot point at a removed row.
        if self.document_lists.currentIndex() == 0 and self.versions.count() > 0:
            self.versions.setCurrentRow(0)
        elif self.document_lists.currentIndex() == 1 and self.cv_versions.count() > 0:
            self.cv_versions.setCurrentRow(0)

    def decide(self, approved):
        if not self.current_version: return
        word = "approve" if approved else "reject"
        if QMessageBox.question(self, "Confirm", f"Do you want to {word} {self.current_version['version_name']}?") == QMessageBox.Yes:
            try:
                self.service.decide(self.current_version["id"], approved); self.refresh()
            except Exception as exc:
                logger.exception("Could not update draft decision")
                QMessageBox.critical(self, "Draft decision", str(exc))

    def open_selected_file_menu(self):
        menu = QMenu(self); pdf_action = menu.addAction("Open PDF"); word_action = menu.addAction("Open Word document")
        chosen = menu.exec(self.open_file_button.mapToGlobal(self.open_file_button.rect().bottomLeft()))
        if chosen == pdf_action:
            self.preview_pdf()
        elif chosen == word_action:
            self.open_docx()

    def preview_pdf(self):
        if self.current_version:
            generated = self.service.paths.resumes_generated / f"{self.current_version['version_name']}.pdf"
            if generated.exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(generated)))
                return
        content = self.current_version["content"] if self.current_version else self.original_content
        if not content: return
        try:
            name = "selected-resume-preview" if self.current_version else "original-preview"; path = self.service.preview_pdf(content, name); QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            QMessageBox.critical(self, "PDF Preview", str(exc))

    def open_docx(self):
        try:
            if self.current_version:
                path = self.service.generated_docx_path(self.current_version["version_name"], bool(self.current_version.get("approved")))
                if not path.exists() and str(self.current_version.get("document_type", "Resume")).upper() == "CV":
                    _, path = self.service.render_version_outputs(self.current_version)
                elif not path.exists():
                    path = self.service.preview_docx(self.current_version["content"], self.current_version["version_name"], json.loads(self.current_version.get("changes_json") or "[]"))
            else:
                if not self.original_content: return
                path = self.service.preview_docx(self.original_content, "original-resume-preview")
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        except Exception as exc:
            QMessageBox.critical(self, "DOCX Export", str(exc))


class ApplicationsPage(QWidget):
    def __init__(self, db: Database):
        super().__init__(); self.db = db; layout = QVBoxLayout(self); layout.setContentsMargins(22, 20, 22, 22); layout.setSpacing(10); layout.addWidget(QLabel("<h1>Applications</h1><p style='color:#6e6e73;margin-top:-7px'>Tracking only. CareerOS never submits applications. Double-click an approved Resume or CV to open it.</p>")); self.table = QTableWidget(0, 8); self.table.setHorizontalHeaderLabels(["Company", "Position", "Match", "Status", "Start date", "Post date", "Resume", "CV"]); self.table.setSelectionBehavior(QAbstractItemView.SelectRows); self.table.setEditTriggers(QAbstractItemView.NoEditTriggers); self.table.cellDoubleClicked.connect(self.open_cell); self.table.itemSelectionChanged.connect(self.update_mark_button); layout.addWidget(self.table); actions = QHBoxLayout(); open_job = QPushButton("Open Job Page"); open_job.clicked.connect(self.open_job); self.mark_button = QPushButton("Mark Selected as Applied"); self.mark_button.clicked.connect(self.mark); actions.addWidget(open_job); actions.addWidget(self.mark_button); actions.addStretch(); layout.addLayout(actions); self.refresh()

    def refresh(self):
        jobs = self.db.application_rows(); self.table.setRowCount(len(jobs))
        for r, j in enumerate(jobs):
            start_date = j.get("start_date") or "Not stated"
            post_date = (j["date_posted"] or "")[:10] or "—"
            for c, value in enumerate((j["company"], j["title"], "—" if j["match_score"] is None else j["match_score"], j["status"], start_date, post_date, j["resume_version"] or "—", j.get("cv_version") or "—")):
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

    def open_cell(self, row: int, column: int):
        if column not in {6, 7}:
            self.open_job(); return
        item = self.table.item(row, column)
        name = item.text().strip() if item else ""
        if not name or name == "—":
            return
        path = get_paths().resumes_approved / f"{name}.pdf"
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, "Approved document", "The approved PDF is not available locally. Regenerate and approve the draft again.")


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
        self.api_key = QLineEdit(); self.api_key.setEchoMode(QLineEdit.Password)
        saved_key = str(api.get("encrypted_key") or "")
        self.api_key.setPlaceholderText("Saved securely for this Windows account" if saved_key.startswith("dpapi:") else "Legacy key saved; use once or re-enter to upgrade" if saved_key else "Paste API key")
        self.clear_key_button = QPushButton("Clear saved API key"); self.clear_key_button.clicked.connect(self.clear_api_key)
        self.api_controls = (self.api_enabled, self.api_url, self.api_model, self.api_key, self.clear_key_button)
        api_form.addRow("API", self.api_enabled); api_form.addRow("Base URL", self.api_url); api_form.addRow("Model", self.api_model); api_form.addRow("API key", self.api_key); api_form.addRow("", self.clear_key_button)
        self.model.currentTextChanged.connect(self.update_api_controls); self.model.currentIndexChanged.connect(self.update_api_controls); self.model.activated.connect(self.update_api_controls)
        self.model.currentTextChanged.connect(lambda _value: QTimer.singleShot(0, self.update_api_controls)); self.update_api_controls(); QTimer.singleShot(0, self.update_api_controls); self.detect_local_models()

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
        data_row = QHBoxLayout(); self.data_dir = QLineEdit(str(get_paths().data_dir)); self.data_dir.setReadOnly(True); browse = QPushButton("Browse..."); browse.clicked.connect(self.browse_data_dir); migrate = QPushButton("Copy data here..."); migrate.clicked.connect(self.migrate_data); data_row.addWidget(self.data_dir, 1); data_row.addWidget(browse); data_row.addWidget(migrate); storage_form.addRow("Data directory", data_row)
        self.merge_database_button = QPushButton("Merge another CareerOS database..."); self.merge_database_button.clicked.connect(self.merge_database); storage_form.addRow("Database merge", self.merge_database_button)
        storage_form.addRow("Auto apply", QLabel("Disabled - manual tracking only")); self.autosave_status = QLabel("Settings save automatically after you stop typing."); self.autosave_status.setStyleSheet("color:#6e6e73"); storage_form.addRow("Status", self.autosave_status)
        credit = QLabel("<p style='color:#6e6e73;margin:4px 2px 0'>CareerOS is a local workspace for manual job discovery, review, and application preparation.</p>"); credit.setWordWrap(True); body_layout.addWidget(credit)
        version = QLabel("CareerOS v0.2.0"); version.setObjectName("versionLabel"); version.setAlignment(Qt.AlignCenter); body_layout.addWidget(version); body_layout.addStretch()
        self.autosave_timer = QTimer(self); self.autosave_timer.setSingleShot(True); self.autosave_timer.timeout.connect(self.autosave)
        autosave_widgets = [self.language, self.model, self.fallback, self.api_enabled, self.api_url, self.api_model, self.api_key, *self.profile_fields.values(), self.search_locations, self.search_queries, self.search_distance, *self.search_sites.values()]
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

    def merge_database(self):
        source, _ = QFileDialog.getOpenFileName(self, "Merge CareerOS Database", str(Path.home()), "CareerOS database (*.db);;SQLite database (*.sqlite *.sqlite3);;All files (*.*)")
        if not source: return
        message = "Merge jobs, drafts, approvals, Applications, and safe in-folder documents from this database?\n\nCurrent data is backed up first. Settings and API keys are never imported."
        if QMessageBox.question(self, "Merge database", message) != QMessageBox.Yes: return
        self.merge_database_button.setEnabled(False); self.autosave_status.setText("Merging database safely...")
        self.merge_worker = TaskThread(self.documents.db.merge_from, source)
        def done(result):
            self.merge_database_button.setEnabled(True); self.autosave_status.setText("Database merge complete."); self.refresh_materials(); self.saved.emit()
            QMessageBox.information(self, "Database merge complete", f"New jobs: {result['jobs']}\nExisting jobs matched: {result['jobs_existing']}\nNew drafts: {result['drafts']}\nExisting drafts skipped: {result['drafts_existing']}\nApplications merged: {result['applications']}\nSupporting documents copied: {result['supporting_documents']}\n\nBackup: {result['backup'] or 'No prior database needed backup'}")
        def failed(error):
            self.merge_database_button.setEnabled(True); self.autosave_status.setText("Database merge failed; current data was not replaced."); QMessageBox.critical(self, "Database merge", user_error_message(error))
        self.merge_worker.completed.connect(done); self.merge_worker.failed.connect(failed); self.merge_worker.start()

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
        # Resume & CV owns these choices now. Preserve its latest independent save
        # when Settings auto-saves other values.
        self.settings["resume_pdf"] = load_settings().get("resume_pdf", self.settings.get("resume_pdf", {}))
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
        self.db = Database(); self.ai = AIManager(self.db); self.jobs_service = JobService(self.db, self.ai); self.resume_service = ResumeService(self.db, self.ai); self.document_service = SupportingDocumentService(self.db)
        shell = QWidget(); shell.setObjectName("appShell"); self.setCentralWidget(shell); layout = QHBoxLayout(shell); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        sidebar = QWidget(); sidebar.setObjectName("sidebar"); sidebar_layout = QVBoxLayout(sidebar); sidebar_layout.setContentsMargins(12, 16, 12, 12); sidebar_layout.setSpacing(11); brand = QWidget(); brand.setObjectName("brand"); brand_layout = QHBoxLayout(brand); brand_layout.setContentsMargins(8, 4, 8, 8); brand_layout.setSpacing(9); brand_icon = QLabel(); brand_icon.setPixmap(self._logo(28)); brand_name = QLabel("<b>CareerOS</b><br/><span style='color:#64748b;font-size:10px'>Career workspace</span>"); brand_layout.addWidget(brand_icon); brand_layout.addWidget(brand_name); brand_layout.addStretch(); sidebar_layout.addWidget(brand)
        self.nav = QListWidget(); self.nav.setObjectName("navList"); self.nav.addItems(["Dashboard", "Jobs", "Resume & CV", "Applications", "Settings"]); self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.nav.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.nav.setFixedHeight(250); sidebar_layout.addWidget(self.nav)
        sidebar_layout.addStretch(1)
        self.task_card = QWidget(); self.task_card.setObjectName("taskCard"); task_layout = QVBoxLayout(self.task_card); task_layout.setContentsMargins(10, 9, 10, 9); task_layout.setSpacing(5)
        task_layout.addWidget(QLabel("<b>Current status</b>"))
        self.global_task_text = QLabel("Ready"); self.global_task_text.setWordWrap(True); self.global_task_text.setObjectName("taskStatusText"); task_layout.addWidget(self.global_task_text)
        self.global_task_progress = QProgressBar(); self.global_task_progress.setTextVisible(False); self.global_task_progress.setMaximumHeight(7); self.global_task_progress.hide(); task_layout.addWidget(self.global_task_progress)
        self.global_task_detail = QLabel(""); self.global_task_detail.setWordWrap(True); self.global_task_detail.setStyleSheet("color:#6e7786;font-size:11px"); task_layout.addWidget(self.global_task_detail)
        self.global_task_cancel = QPushButton("Cancel"); self.global_task_cancel.setEnabled(False); self.global_task_cancel.clicked.connect(self.cancel_active_task); task_layout.addWidget(self.global_task_cancel)
        sidebar_layout.addWidget(self.task_card); sidebar.setFixedWidth(208)
        right = QWidget(); right.setObjectName("rightPane"); right_layout = QVBoxLayout(right); right_layout.setContentsMargins(0, 0, 0, 0); right_layout.setSpacing(0); top_bar = QWidget(); top_bar.setObjectName("topBar"); top_bar.setFixedHeight(40); top_layout = QHBoxLayout(top_bar); top_layout.setContentsMargins(17, 0, 17, 0); top_label = QLabel("<b>CareerOS</b>"); top_label.setObjectName("topTitle"); top_layout.addWidget(top_label); top_layout.addStretch(); right_layout.addWidget(top_bar)
        self.stack = QStackedWidget(); self.dashboard = DashboardPage(self.db); self.jobs = JobsPage(self.db, self.jobs_service, self.resume_service, self.ai); self.resume = ResumePage(self.db, self.resume_service); self.applications = ApplicationsPage(self.db); self.settings = SettingsPage(self.resume_service, self.document_service, self.ai)
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
            QWidget#taskCard{background:rgba(255,255,255,150);border:1px solid #cbd3df;border-radius:10px} QLabel#taskStatusText{font-size:11px;color:#334155}
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
            QLineEdit:disabled,QComboBox:disabled,QTextEdit:disabled,QSpinBox:disabled{background:#e5e7eb;color:#9aa1ad;border-color:#d7dbe3}
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
        self.stack.setCurrentIndex(index); page = self.stack.currentWidget(); effect = QGraphicsOpacityEffect(page); page.setGraphicsEffect(effect); animation = QPropertyAnimation(effect, b"opacity", self); animation.setDuration(170); animation.setStartValue(0.72); animation.setEndValue(1.0); animation.setEasingCurve(QEasingCurve.OutCubic); animation.finished.connect(lambda: page.setGraphicsEffect(None)); self.page_animation = animation; animation.start(); self.refresh_current_page()

    def refresh_current_page(self):
        page = self.stack.currentWidget()
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    def refresh_pages(self):
        # UI navigation used to synchronously refresh every page.  Resume.refresh
        # can involve PDF loading and (on the first preview) Word COM, so doing
        # all pages on every navigation made the whole application stutter.
        # Pages refresh when they become active; background pages remain idle.
        self.refresh_current_page()
    def settings_changed(self): self.jobs.refresh_models(); self.jobs.refresh_location_choices(); self.refresh_pages()

    def begin_task_state(self, worker: TaskThread, text: str) -> None:
        self.active_task = worker; self.global_task_text.setText(text); self.global_task_detail.setText("Starting…")
        self.global_task_progress.setRange(0, 0); self.global_task_progress.show(); self.global_task_cancel.setEnabled(True)

    def update_task_state(self, text: str, _started_at=None) -> None:
        self.global_task_text.setText(text)

    def set_task_progress(self, completed: int, total: int, detail: str) -> None:
        self.global_task_progress.setRange(0, max(1, total)); self.global_task_progress.setValue(completed); self.global_task_progress.show(); self.global_task_detail.setText(detail)

    def finish_task_state(self, detail: str = "Ready") -> None:
        self.global_task_text.setText("Ready"); self.global_task_detail.setText(detail); self.global_task_progress.hide(); self.global_task_cancel.setEnabled(False); self.active_task = None

    def cancel_active_task(self) -> None:
        worker = getattr(self, "active_task", None)
        if not worker or not worker.isRunning():
            self.finish_task_state("No task is running.")
            return
        worker.requestInterruption(); self.global_task_text.setText("Cancelling current task…"); self.global_task_detail.setText("CareerOS will stop after the current safe step."); self.global_task_cancel.setEnabled(False)


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

    # Pre-render and load the original resume while the splash screen is still
    # visible.  Word COM stays off the GUI thread, and QPdfDocument.load runs
    # back on the GUI thread before the workspace appears.  The splash remains
    # for at least three seconds and, normally, until the preview is ready.
    preview_worker = TaskThread(window.resume_service.prepare_original_preview)
    preview_state = {"done": False}
    def preview_ready(path):
        preview_state["done"] = True
        window.resume.load_prepared_original_preview(path)
    def preview_failed(error):
        preview_state["done"] = True
        logger.warning("Startup resume preview preparation failed: %s", error.splitlines()[0] if error else "unknown error")
    preview_worker.completed.connect(preview_ready); preview_worker.failed.connect(preview_failed); preview_worker.start()
    window.startup_preview_worker = preview_worker

    wait_loop = QEventLoop()
    poll = QTimer(); poll.setInterval(40)
    def maybe_finish_startup():
        # Do not let a broken Word installation hold the splash forever.
        if (elapsed.elapsed() >= 3000 and preview_state["done"]) or elapsed.elapsed() >= 8000:
            poll.stop(); wait_loop.quit()
    poll.timeout.connect(maybe_finish_startup); poll.start(); maybe_finish_startup(); wait_loop.exec()

    window.show(); splash.finish(window)
    return app.exec()
