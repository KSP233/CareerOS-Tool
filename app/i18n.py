from __future__ import annotations

from PySide6.QtWidgets import QAbstractButton, QComboBox, QLabel, QListWidget, QMainWindow, QTabWidget, QTableWidget

from config import load_settings


ZH = {
    "Dashboard": "仪表盘", "Jobs": "职位", "Resume & CV": "简历与求职信", "Applications": "申请记录", "Settings": "设置",
    "Your job search at a glance.": "求职进度一览。", "Search, review and organize opportunities.": "搜索、审核和整理职位机会。",
    "Review resume drafts and generated cover letters. Red is removed; green is new.": "审核简历草稿和求职信。红色为删除内容；绿色为新增内容。",
    "Tracking only. CareerOS never submits applications.": "仅用于追踪。CareerOS 不会提交申请。",
    "Tip: use Ctrl or Shift to select multiple jobs (maximum 20 per batch).": "提示：按住 Ctrl 或 Shift 可选择多个职位（每批最多 20 个）。",
    "Search Jobs": "搜索职位", "Add Job": "添加职位", "Import": "导入", "Analyze Selected": "分析所选职位", "Resume Drafts": "简历草稿", "Cover Drafts": "求职信草稿",
    "Open Job Page": "打开职位网页", "Translate 中文": "翻译为中文", "Show Chinese": "显示中文", "Original text": "原文",
    "Open Selected PDF": "打开所选 PDF", "Approve Version": "批准版本", "Reject Version": "拒绝版本", "Cover Letters": "求职信",
    "Regenerate": "重新生成", "Generating...": "正在生成...", "CV Draft": "CV 草稿",
    "Delete Selected Draft": "删除所选草稿", "Unmark as Applied": "取消标记为已申请",
    "Generation instructions": "生成提示词", "Resume Draft": "简历草稿", "Cover Letter": "求职信", "Status": "状态", "Preview": "预览",
    "Company": "公司", "Position": "职位", "Match": "匹配度", "Location": "地点", "Source": "来源", "Salary": "薪资",
    "Start date": "开始日期", "Post date": "发布日期", "Date": "日期", "Resume": "简历", "All locations": "所有地点", "All sources": "所有来源", "All matches": "所有匹配", "All statuses": "所有状态",
    "Mark Selected as Applied": "标记所选职位为已申请", "Application form details": "申请表资料", "Verified candidate information": "已验证的候选人资料",
    "Job search": "职位搜索", "Match score weighting": "匹配分数权重", "Storage & safety": "存储与安全", "AI provider": "AI 提供方", "External API": "外部 API", "Resume": "简历",
    "Locations": "地点", "Job types": "职位类型", "Sources": "来源", "Distance": "距离", "Search plan": "搜索计划", "Rule score": "规则分数", "AI analysis": "AI 分析", "Formula": "公式", "Default AI": "默认 AI", "Local model 1": "本地模型 1", "Local model 2": "本地模型 2", "Local fallback": "本地模型回退",
    "PDF style": "PDF 风格", "Body font size": "正文字号", "Page margins": "页边距", "Data directory": "数据目录", "Auto apply": "自动投递", "Language": "语言",
    "Modern": "现代", "Classic": "经典", "Compact": "紧凑", "Narrow": "窄", "Standard": "标准", "Comfortable": "宽松", "Fit width": "适合宽度",
    "New": "新职位", "Interested": "感兴趣", "Review": "审核中", "Preparing": "准备中", "Ready": "就绪", "Applied": "已申请", "Interview": "面试", "Rejected": "已拒绝", "Offer": "录用", "Ignored": "忽略",
    "AI: Ready": "AI：就绪", "AI: Offline": "AI：离线", "Starting task...": "正在启动任务...", "Error": "发生错误",
    "Refresh local models": "刷新本地模型", "Detecting installed local models...": "正在检测已安装的本地模型...", "No local models detected; saved choices are shown.": "未检测到本地模型；显示已保存的选择。", "Could not detect local models.": "无法检测本地模型。",
    "Analyzing ": "正在分析 ", "Searching ": "正在搜索 ", "Resume ": "正在生成简历 ", "Cover letter ": "正在生成求职信 ",
    "Progress ": "进度 ", "elapsed ": "已用时 ", "about ": "预计约 ", "remaining": "剩余", "estimating time remaining...": "正在估算剩余时间...", "Completed in ": "已完成，用时 ",
    "Searching...": "正在搜索...", "Analyzing ": "正在分析 ", "Generating ": "正在生成 ", "Translation ready: ": "翻译完成：", "Translating to Chinese...": "正在翻译为中文...", "Another task is still running": "另一个任务仍在运行中",
}


def current_language() -> str:
    """Read the persisted language once per explicit localization pass."""
    return str(load_settings().get("language", "en"))


def translate(text: str, language: str | None = None) -> str:
    if (language or current_language()) != "zh":
        return text
    for source, target in sorted(ZH.items(), key=lambda item: len(item[0]), reverse=True):
        text = text.replace(source, target)
    return text


def _original(owner, key: str, value: str) -> str:
    saved = owner.property(key)
    if saved is None:
        owner.setProperty(key, value)
        return value
    return str(saved)


def localize_widget_tree(root, language: str | None = None) -> None:
    """Translate known static UI text while retaining the original for English switching."""
    language = language or current_language()
    root.setProperty("_careeros_language", language)
    if isinstance(root, QMainWindow):
        root.setWindowTitle(translate(_original(root, "_careeros_title", root.windowTitle()), language))
    for widget in root.findChildren(QLabel):
        widget.setText(translate(_original(widget, "_careeros_text", widget.text()), language))
    for widget in root.findChildren(QAbstractButton):
        widget.setText(translate(_original(widget, "_careeros_text", widget.text()), language))
    for widget in root.findChildren(QComboBox):
        originals = widget.property("_careeros_items")
        if originals is None:
            originals = [widget.itemText(index) for index in range(widget.count())]
            widget.setProperty("_careeros_items", originals)
        for index, value in enumerate(originals):
            widget.setItemText(index, translate(str(value), language))
    for widget in root.findChildren(QListWidget):
        for index in range(widget.count()):
            item = widget.item(index)
            original = item.data(0x100 + 1000)
            if original is None:
                original = item.text()
                item.setData(0x100 + 1000, original)
            item.setText(translate(str(original), language))
    for widget in root.findChildren(QTabWidget):
        originals = widget.property("_careeros_tabs")
        if originals is None:
            originals = [widget.tabText(index) for index in range(widget.count())]
            widget.setProperty("_careeros_tabs", originals)
        for index, value in enumerate(originals):
            widget.setTabText(index, translate(str(value), language))
    for widget in root.findChildren(QTableWidget):
        originals = widget.property("_careeros_headers")
        if originals is None:
            originals = [widget.horizontalHeaderItem(index).text() if widget.horizontalHeaderItem(index) else "" for index in range(widget.columnCount())]
            widget.setProperty("_careeros_headers", originals)
        for index, value in enumerate(originals):
            item = widget.horizontalHeaderItem(index)
            if item:
                item.setText(translate(str(value), language))
