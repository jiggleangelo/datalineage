import re
import textwrap

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _safe_lines(lines, width=92):
    output = []
    for value in lines:
        text = str(value).replace("\r", " ").replace("\n", " ")
        text = _CONTROL.sub(" ", text)
        text = text.encode("cp1252", "replace").decode("latin-1")
        wrapped = textwrap.wrap(text, width=width, replace_whitespace=True, drop_whitespace=True) or [""]
        for line in wrapped:
            output.append(line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)"))
    return output


def make_pdf(lines):
    safe = _safe_lines(lines)
    pages = [safe[i:i + 50] for i in range(0, len(safe), 50)] or [[]]
    objects = ["<< /Type /Catalog /Pages 2 0 R >>"]
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(len(pages)))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for index, page in enumerate(pages):
        content_no = 5 + index * 2
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_no} 0 R >>")
        commands = ["BT /F1 9 Tf 45 800 Td 12 TL"] + [f"({line}) Tj T*" for line in page] + ["ET"]
        stream = "\n".join(commands)
        objects.append(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
    result = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n{obj}\nendobj\n".encode("latin-1"))
    start = len(result)
    result.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(result)
