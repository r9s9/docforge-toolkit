"""Safe XML parsing helpers.

DOCX parts are untrusted input. We parse with a hardened lxml parser that:
  * does not resolve entities  -> blocks "billion laughs" / entity expansion
  * does not load DTDs          -> blocks external DTD tricks
  * does not hit the network    -> blocks XXE external fetches
"""

from __future__ import annotations

from lxml import etree


def safe_xml_parser() -> etree.XMLParser:
    """Return a parser hardened against XXE and entity-expansion attacks."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        recover=False,
    )


def parse_xml(data: bytes) -> etree._Element:
    """Parse XML bytes safely. Raises lxml.etree.XMLSyntaxError on bad input."""
    return etree.fromstring(data, parser=safe_xml_parser())
