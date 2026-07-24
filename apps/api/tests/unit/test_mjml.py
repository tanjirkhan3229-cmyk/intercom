"""Unit tests for outbound email rendering (no DB): variable substitution, escaping, MJML subset."""

from __future__ import annotations

from relay.modules.outbound.mjml import render_email, substitute


def test_variable_substitution_and_escaping() -> None:
    ctx = {"contact": {"name": "Ada"}, "plan": "Pro"}
    assert substitute("Hi {{ contact.name }} on {{ plan }}", ctx) == "Hi Ada on Pro"
    # A value containing markup is HTML-escaped (never injects tags).
    injected = substitute("{{ contact.name }}", {"contact": {"name": "<b>x</b>"}})
    assert injected == "&lt;b&gt;x&lt;/b&gt;"
    # Missing variables collapse to empty string.
    assert substitute("[{{ missing }}]", {}) == "[]"


def test_mjml_subset_renders_to_html_and_text() -> None:
    template = (
        "<mjml><mj-body><mj-section><mj-column>"
        "<mj-text>Hello {{ contact.name }}</mj-text>"
        '<mj-button href="https://x.test">Go</mj-button>'
        "</mj-column></mj-section></mj-body></mjml>"
    )
    rendered = render_email(template=template, context={"contact": {"name": "Ada"}})
    assert "Hello Ada" in rendered.html
    assert '<a href="https://x.test">Go</a>' in rendered.html
    assert "<table" in rendered.html  # mj-section → table
    assert "Hello Ada" in rendered.text
    assert "<" not in rendered.text  # text part is tag-free


def test_variable_markup_cannot_inject_on_mjml_path() -> None:
    """Regression: a variable value stays HTML-escaped in the MJML path (no parser re-decode)."""
    template = "<mjml><mj-body><mj-text>{{ name }}</mj-text></mj-body></mjml>"
    rendered = render_email(
        template=template, context={"contact": {}, "name": "<script>alert(1)</script>"}
    )
    assert "<script>" not in rendered.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered.html


def test_plain_html_passthrough() -> None:
    rendered = render_email(
        template="<p>Hi {{ contact.name }}</p>", context={"contact": {"name": "Zoe"}}
    )
    assert rendered.html == "<p>Hi Zoe</p>"
    assert rendered.text == "Hi Zoe"
