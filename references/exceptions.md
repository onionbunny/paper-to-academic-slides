# 异常处理手册（Exception Catalog）

原则：先识别、再降级、全程记录。任何降级产物都要在 `result.json` 的 `status/uncertainty/warnings` 中如实标注；部分结果必须写明局限。禁止编造缺失的软件状态或科学数据。

## 1. PDF 加密或有打开密码

- 检测：`fitz.Document.needs_pass == True`。
- 处理：先用空密码 `authenticate("")`；失败则不再尝试穷举，阶段中止并返回 `status=failed/partial`，在 errors 中写明“encrypted PDF, password rejected”，同时继续处理输入中其他可用素材（如任务说明、模板）。
- 若 case 材料中提供了密码字段，读取后再认证，并在变更记录中注明使用了案例提供的密码。

## 2. PDF 损坏或无法打开

- 检测：`fitz.open` 抛异常或 `page_count==0`。
- 处理：记录错误；尝试用 `pdfplumber`/`pdftotext` 二次打开作为降级；仍失败则输出不含论文内容的 partial 结果，缺失项逐条列出，不生成占位性“论文内容”。

## 3. 扫描件 / 无文本层

- 检测：页面可提取文本极少而整页图像占比高。
- 处理：标记 `scanned=true`；不臆造 OCR 结论。若环境存在 OCR 工具（如 `tesseract`，先 `which tesseract` 验证）可运行 OCR 并将结果标注为“OCR 文本，可能有误”，证据定位回退为页码 + 整页渲染图；无 OCR 时仅交付基于页面图像的幻灯，并在 uncertainty 中说明文字不可检索。

## 4. 图表无法提取 / 无内嵌图

- 检测：`get_images` 为空，或矢量图无法通过图形元素定位。
- 处理：脚本自动回退到 200 DPI 区域裁剪（caption 上方区域），再不行使用整页渲染图并在备注中指明“整页截图，证据见该页”；资产标记为 fallback，QA 不视为伪造，但 uncertainty 要说明清晰度与定位局限。

## 5. 公式为图片或特殊符号无法编码

- 检测：公式区域无文本、字形为私有字体（如 MT Extra/Cambria Math 替换风险）。
- 处理：保持公式为图片裁剪，不手工重排符号；备注中用文字解释公式含义与变量；涉及数值结论时引用正文已计算好的结果，不自行代入计算。

## 6. 模板占位符不足或版式不匹配

- 检测：`template.json` 中找不到期望 `layout_role`，或版式无 BODY/PICTURE 占位符。
- 处理：构建脚本按优先级回退——①选用占位符集合最接近的真实版式；②在安全区内添加文本框/图片框（几何取自 `safe_area`，字体走主题或字体映射）；③最后才使用空白版式。回退路径写入变更记录，禁止覆盖母版与主题色。

## 7. 字体缺失 / 跨平台替换

- 检测：构建前 `resolve_font` 发现候选字体未安装；渲染后 QA 发现嵌入字体不含期望族名。
- 处理：按映射表替换为度量兼容字体（微软雅黑/黑体→Noto Sans CJK SC；宋体/仿宋/楷体→Noto Serif CJK SC；Calibri→Carlito；Arial/Helvetica→Liberation Sans；Times→Liberation Serif；Cambria→Caladea），并在环境记录中列出替换关系。内容为中文而无 CJK 字体嵌入时按高风险告警，防止豆腐块。

## 8. 页数上限冲突

- 检测：计划页数 > `max_slides`。
- 处理：validate 直接报 error；按证明力裁剪——保标题、问题、方法、主结论证据、结论页，合并支撑性细节与备份页；被删条目写入 uncertainty（含编号与页码），保证信息可追溯但不违反冻结约束。

## 9. 长标题 / 文本溢出

- 检测：构建脚本确定性适配在字号下限仍无法容纳；QA 在渲染 PDF 上检测到形状越界。
- 处理顺序：①改写为更短的结论短语（事实不变）；②脚本在允许范围内收缩字号（标题下限 18pt、正文 11pt、表格 9pt）；③拆分到两页或改用分栏；仍失败则保留并在 warnings 标注具体页与越界类型，禁止让文字默默出血。

## 10. 缺少模板或起始 deck

- 检测：输入中无 PPTX。
- 处理：使用脚本内置的中性学术默认规格（16:9，配色与字体见 `default_theme.md`），在环境记录注明“no template supplied; default theme used”；一旦案例后补模板，重跑构建即可，计划无需改动。

## 11. LibreOffice 失败或 profile 锁定

- 检测：转换返回非零、无 PDF 输出。
- 处理：使用独立 `-env:UserInstallation` 配置目录避免并发锁；清理残留进程后重试一次；仍失败则保留可编辑 PPTX 并将渲染状态标记为失败（`rendered=false` 及原因），result 状态降为 partial，不伪造预览。

## 12. 多份 PDF / 多份 deck

- 检测：素材盘点出现多个同类文件。
- 处理：按文件名提示（template/start/模板/底稿）与体积判定主论文与模板；无法判定时在 warnings 全量列出并默认最大 PDF 为主论文、首个 deck 为模板；不删除任何用户文件。

## 13. 编码与乱码

- 检测：JSON/文本读取出现解码错误。
- 处理：按 UTF-8、UTF-8 BOM、GB18030、Latin-1 顺序尝试；写出统一使用 UTF-8；原始字节不被修改。
