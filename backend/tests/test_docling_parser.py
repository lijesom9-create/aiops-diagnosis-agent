"""Tests for DoclingParser"""

import pytest


class TestDoclingParser:
    def setup_method(self):
        try:
            from backend.app.document.docling_parser import DoclingParser
            self.parser = DoclingParser()
        except ImportError:
            pytest.skip("Docling not installed")

    def test_supported_extensions(self):
        from backend.app.document.docling_parser import DoclingParser
        assert ".pdf" in DoclingParser.SUPPORTED_EXTENSIONS
        assert ".docx" in DoclingParser.SUPPORTED_EXTENSIONS

    def test_parse_simple_pdf(self):
        """用 fpdf2 生成简单 PDF 并解析"""
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, "Test Title", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 12)
        pdf.cell(0, 10, "Hello world paragraph.", new_x="LMARGIN", new_y="NEXT")
        pdf_path = "/tmp/test_simple.pdf"
        pdf.output(pdf_path)
        with open(pdf_path, "rb") as f:
            content = f.read()
        doc = self.parser.parse(content, "test_simple.pdf")
        assert doc.metadata.filename == "test_simple.pdf"
        assert len(doc.elements) > 0

    def test_parse_with_table(self):
        """解析含表格的 PDF（Docling 自动检测表格）"""
        from fpdf import FPDF
        pdf = FPDF()
        pdf.add_page()
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, "Data Table", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 10)
        for row in [["A", "1"], ["B", "2"]]:
            pdf.cell(20, 8, row[0], border=1)
            pdf.cell(20, 8, row[1], border=1)
            pdf.ln()
        pdf_path = "/tmp/test_table.pdf"
        pdf.output(pdf_path)
        with open(pdf_path, "rb") as f:
            content = f.read()
        doc = self.parser.parse(content, "test_table.pdf")
        # 确保文档被解析了（表格检测质量取决于 Docling 模型）
        assert len(doc.elements) > 0

    def test_parse_empty_content(self):
        doc = self.parser.parse(b"", "empty.pdf")
        assert len(doc.elements) == 0
