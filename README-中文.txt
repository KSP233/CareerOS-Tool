CareerOS 本地 AI 求职助手

程序目录：CareerOS 程序所在目录
数据目录：%LOCALAPPDATA%\CareerOS\data

日常使用：双击 START.cmd。
出现问题：双击 START-DEBUG.cmd 查看错误。

主要功能：
- 查看、添加和搜索职位
- 搜索/导入后立即显示 ~规则初评分；点 Analyze 后生成 70% 规则 + 30% AI 正式评分
- Match 紧跟 Company；正式 AI 分数显示绿色粗体，~初评分保持普通颜色
- Jobs 表格支持 Ctrl/Shift 多选，最多 20 个一批执行 Analyze、Resume Drafts 或 Cover Drafts
- 批量 Resume 和 Cover 只生成本地草稿；Resume 仍需逐个批准，绝不自动申请
- CSV / JSON 职位导入、去重、筛选和排序
- Job Details 显示发布日期、明确的开始时间、学历要求和其他要求
- 要求旁显示 Likely met / Not confirmed / Does not meet
- Settings 可填写用户确认的技能、学历、经验、工签、语言、证照、求职方向、地点、薪资和补充事实
- Settings 可导入 TXT、Markdown、PDF 或 Word (.docx) 简历；原文件保留，只在本地提取文字
- Settings 的 Additional Materials 可一次上传多个额外资料；PDF、Word、Excel、文本和常见数据文件会在本地提取，供 Analyze、Resume Drafts、Cover Drafts 参考
- 其它文件格式也可保存，但会明确标为“stored; not readable”，不会假装被 AI 读取
- Settings 可逐行输入任意搜索地点，并调整每个地点周边的英里距离
- Data Directory 可浏览选择；切换时复制数据库和文件，旧目录保留，重启后生效
- Full Description 会自动清理抓取产生的重复空行和 Markdown 符号
- Translate 中文由用户手动触发，使用本地 qwen3.5:9b，结果缓存在数据库；Show Chinese 可切回原文
- Auto / gpt-oss:20b / qwen3.5:9b 本地模型切换和一次 Fallback
- 可选 OpenAI-compatible API（Base URL、模型、API Key）；Auto 仍只用本地模型
- API Key 加密保存在本机；每次选择 API 分析/生成前都会提示将发送哪些内容
- Original Resume 只读保护
- Resume Drafts 和 Cover Letter 都输出为带排版的 PDF；可先打开 PDF Preview，再保存 PDF Draft
- 针对职位生成逐条 Resume 修改建议、比较、PDF 预览、批准或拒绝
- 手动 Application Tracker
- Applications 中选中职位后可点 Open Job Page（或双击行）打开原始职位链接
- Settings 中的文字、选项和搜索来源会在停止输入约半秒后自动保存；数据目录迁移仍需点 Copy data here... 明确确认
- JobSpy 目前可在 Settings 选择 Indeed、LinkedIn、Google Jobs、Glassdoor 和 ZipRecruiter；每个来源都有自身网络限制，搜索失败会显示为警告而不会投递申请
- Resume & CV 页面可分别填写 Resume Draft 和 Cover Letter 的自定义生成提示词；它们只保存在本地，并在下一次生成草稿时追加给 AI
- Settings 的 Resume 区可选择 PDF 风格（Modern、Classic、Compact）、正文 8-12 pt 字号和页边距；内置预览支持连续多页滚动，生成的 PDF 不再含 CareerOS 页脚
- Salary 优先使用职位来源返回的工资字段；字段为空时会从职位描述中提取金额范围，并为多省份范围标注 varies by location。Resume & CV 预览默认 80%，可切换 65%、100% 或 Fit width。
- Applications 显示职位的明确 Start date 与来源返回的 Post date。Settings 可选 English 或 中文，切换只改变界面文字，不会翻译或修改已保存的职位数据。
- Start date 会在导入或职位描述更新时缓存，Applications 刷新不再逐条解析长描述。Resume & CV 选中已有草稿后，可选择重新生成 Resume Draft 或 CV Draft；两者都仅生成本地草稿，仍须人工审核和批准。
- Resume & CV 的 Regenerate 会自动沿用当前选中草稿的 Resume/CV 类型；可删除未批准的草稿或求职信（必须确认）。Applications 的已申请标记可取消，并恢复标记前的本地状态。
- Settings 的 Match score weighting 可调节 Rule score 与 AI analysis 的占比；两个百分比自动合计 100%，改动后需重新 Analyze 才会按新公式计算职位分数
- 可选 Form Fill Assistant：在 Settings 填写姓名、邮箱、电话和地址后，选中职位并点击 Copy Form Fill Script；用户自行打开已登录的申请页，在浏览器开发者控制台粘贴并运行脚本，脚本只填写空白文本字段

默认搜索范围：Ottawa、Montreal、Toronto、Quebec City 周边 30 miles，最近 72 小时；可在 Settings 修改地点和距离。
“~65%”表示快速规则初评分；“65%”表示已完成 AI 分析的正式评分。

安全边界：
- Form Fill Assistant 只在用户手动运行后填写文本字段；不会点击、勾选、上传文件、回答筛选题或提交表单
- 没有自动上传或自动提交代码；每份申请都必须由用户逐项检查并手动点击 Submit
- AI 只能生成草稿，Resume 必须由用户确认
- Original Resume 永远不会被覆盖
- 个人数据、数据库和输出默认只保存在 %LOCALAPPDATA%\CareerOS\data，或你在 Settings 选择的位置
- 只有主动选择 API 并确认后，相关简历/资料和职位描述才会发送给配置的 API
