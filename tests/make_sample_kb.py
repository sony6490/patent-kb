"""生成样例知识库文件（docx/xlsx/csv/pptx/txt/图片），用于端到端测试。"""
import sys
from pathlib import Path

target = Path(sys.argv[1] if len(sys.argv) > 1 else "D:/WorkBuddy/2026-11-18-15-20-21/patent-kb/tests/sample_kb")
target.mkdir(parents=True, exist_ok=True)
sub = target / "无线充电项目"
sub.mkdir(exist_ok=True)

# 1. docx
from docx import Document
doc = Document()
doc.add_heading("电动汽车无线充电定位误差补偿检索报告", level=1)
doc.add_paragraph(
    "本项目针对电动汽车无线充电系统中接收线圈与发射线圈之间因车辆停位偏差导致的耦合系数下降问题进行现有技术检索。"
    "核心技术特征包括：线圈定位误差补偿、磁耦合机构、Qi 协议通信、异物检测 FOD（Foreign Object Detection）。"
    "初步结论：H02J 50/80 涉及按用途或目的分类的无线充电电路装置；G01B 系列涉及位移测量。"
)
doc.add_paragraph(
    "检索中发现的关键技术路线：① 基于磁场的定位检测线圈阵列；② 基于摄像头视觉的地面标线识别；"
    "③ 基于超声波传感器的测距补偿。相关关键词包括：wireless power transfer, coil alignment, "
    "positioning tolerance, misalignment compensation, 磁耦合共振, 谐振频率偏移。"
)
table = doc.add_table(rows=3, cols=2)
table.rows[0].cells[0].text = "检索要素"
table.rows[0].cells[1].text = "关键词"
table.rows[1].cells[0].text = "要素A 定位"
table.rows[1].cells[1].text = "线圈 阵列 磁场 强度"
table.rows[2].cells[0].text = "要素B 补偿"
table.rows[2].cells[1].text = "误差 补偿 校准 偏移"
doc.save(str(sub / "无线充电检索报告.docx"))

# 2. xlsx
from openpyxl import Workbook
wb = Workbook()
ws = wb.active
ws.title = "筛选结果"
ws.append(["公开号", "标题", "申请人", "相关度", "备注"])
ws.append(["CN100000001A", "一种白光数码瞄准镜激光测距光轴可调机构", "示例公司甲", "B", "球面配合+止螺锁定"])
ws.append(["CN100000002A", "无线充电线圈定位装置及电动车", "示例公司乙", "A", "检测线圈阵列"])
ws.append(["US20260000001A1", "Coil alignment compensation for wireless charging", "Example Corp", "A", "misalignment"])
ws.append(["EP900000001A2", "Positioning system for inductive charging", "Example GmbH", "B", "ultrasonic"])
wb.save(str(sub / "筛选结果表.xlsx"))

# 3. csv
csv_path = sub / "标引记录.csv"
csv_path.write_text(
    "公开号,IPC,技术分支\n"
    "CN100000002A,H02J50/80,线圈定位\n"
    "US20260000001A1,H02J50/90,误差补偿\n"
    "EP900000001A2,H02J50/10,谐振电路\n",
    encoding="utf-8",
)

# 4. pptx
from pptx import Presentation
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[1])
slide.shapes.title.text = "无线充电 FTO 项目阶段汇报"
slide.placeholders[1].text = (
    "客户需求：美国市场无线充电模块 FTO 排查。核心专利族：H02J50 系列与 B60L 53/00。"
    "重点关注 主要厂商的 SEP 许可态势，FRAND 谈判窗口预计在明年 Q2。"
)
prs.save(str(sub / "阶段汇报.pptx"))

# 5. txt
(target / "检索历史.txt").write_text(
    "一、无效检索目标：DE1000000000C5\n"
    "权利要求1：一种无线充电定位方法，其特征在于包括如下步骤：\n"
    "S1 检测发射线圈与接收线圈的相对位置偏差；\n"
    "S2 根据偏差调整补偿电流；S3 在谐振频率偏移超过阈值时触发告警。\n"
    "二、对比文件线索：JP2000000000A 公开了检测线圈阵列定位的方案。\n"
    "三、分类号解释：H02J50/80 覆盖用于电动车辆充电的感应式功率传输装置。\n",
    encoding="utf-8",
)

# 6. md
(target / "README.md").write_text(
    "# 知识库使用说明\n\n本文件夹是示例知识库，包含历史项目检索报告、筛选表、标引记录与阶段汇报。\n",
    encoding="utf-8",
)

# 7. 图片（1x1 PNG 伪文件即可）
png = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000a49444154789c6300010000050001"
    "0d0a2db40000000049454e44ae426082"
)
# 手工构造最小 PNG
import struct, zlib
def make_png():
    sig = b"\x89PNG\r\n\x1a\n"
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        c += struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
        return c
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    raw = b"\x00\xff\x00\x00"
    idat = chunk(b"IDAT", zlib.compress(raw))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend
(target / "附图_线圈布置.png").write_bytes(make_png())

# 8. 老格式占位（doc 内容不重要，仅验证 unsupported 标记）
(target / "旧版报告.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)

print("样例库已生成:", target)
for f in sorted(target.rglob("*")):
    if f.is_file():
        print(" ", f.relative_to(target), f.stat().st_size, "bytes")
