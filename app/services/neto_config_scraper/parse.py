"""Pure HTML parsers for Neto cPanel Advanced Configuration pages.

Two pages (under https://www.chainsawspares.com.au/_cpanel/):
  config?item=config&max=250&pagenum=N   -> parse_list      (the variable list)
  config/view?id=KEY&mod=MOD             -> parse_detail     (one variable)

No network, no session here — feed raw HTML so they're testable against saved
fixtures. Confirmed live 2026-06-03 (1147 variables across 5 pages of 250).
"""
from __future__ import annotations

import re
from bs4 import BeautifulSoup

_VIEW_RE = re.compile(r"config/view\?id=")
# A real list row's Module cell is a short uppercase token (MAIN, SECURE,
# CPANEL…). This rejects the wrapper/filter rows that also contain a view link.
_MOD_TOKEN_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,30}$")


def _id_mod(href: str) -> tuple[str | None, str | None]:
    kid = re.search(r"[?&]id=([^&]+)", href)
    mod = re.search(r"[?&]mod=([^&]+)", href)
    return (kid.group(1) if kid else None, mod.group(1) if mod else None)


def parse_list(html: str) -> list[dict]:
    """Parse one config list page. Columns: [chk, Module, Name, Title, Value, Type].

    ``Type`` is text like ``[System]`` / ``[Read-only] [System]`` / ``[Custom]``.
    Secret values arrive pre-masked by Neto (``****************``).
    """
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for r in soup.find_all("tr"):
        link = r.find("a", href=_VIEW_RE)
        if not link:
            continue
        cells = [td.get_text(" ", strip=True) for td in r.find_all("td")]
        if len(cells) < 6:
            continue
        module = (cells[1] or "").strip()
        name = (cells[2] or "").strip()
        if not _MOD_TOKEN_RE.match(module):
            continue  # wrapper/filter row, not a data row
        if link.get_text(strip=True) != name:
            continue
        kid, mod = _id_mod(link["href"])
        type_raw = (cells[5] or "").strip()
        out.append({
            "module": module,
            "name": name,
            "title": (cells[3] or "").strip(),
            "value": (cells[4] or "").strip(),
            "type_raw": type_raw,
            "is_system": "[system]" in type_raw.lower(),
            "is_readonly": "read-only" in type_raw.lower(),
            "is_custom": "[custom]" in type_raw.lower(),
            "config_id": kid or name,
            "mod": (mod or module.lower()),
        })
    return out


def _masked(value: str) -> bool:
    v = (value or "").strip()
    return bool(v) and set(v) == {"*"}


def parse_detail(html: str) -> dict:
    """Extract the richer per-variable info the list doesn't carry:
    full description, the value-editor data type, and enum options."""
    soup = BeautifulSoup(html, "html.parser")

    data_type, options = "text", []
    el = soup.find(["select", "textarea", "input"], attrs={"name": "value"})
    if el is not None:
        if el.name == "select":
            data_type = "enum"
            options = [{
                "value": o.get("value"),
                "label": o.get_text(strip=True),
                "selected": o.has_attr("selected"),
            } for o in el.find_all("option")]
        elif el.name == "textarea":
            data_type = "textarea"
        else:
            t = (el.get("type") or "text").lower()
            data_type = "boolean" if t in ("checkbox", "radio") else t

    # Description: the "Description:" label in the Configuration Description block.
    description = None
    for lab in soup.find_all(string=re.compile(r"^\s*Description:\s*$")):
        sib = lab.parent.find_next(["td", "div", "span", "p"])
        if sib:
            description = re.sub(r"\s+", " ", sib.get_text(" ", strip=True)).strip()
        break
    if not description:
        m = re.search(r"Description:\s*</[^>]+>\s*(?:<[^>]+>\s*)?([^<]{2,500})", html)
        if m:
            description = re.sub(r"\s+", " ", m.group(1)).strip()

    return {
        "data_type": data_type,
        "options": options,
        "description": description or None,
    }
