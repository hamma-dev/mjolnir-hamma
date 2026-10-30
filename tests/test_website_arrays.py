"""hamma.dev's per-array overview pages (HAMMA / AUMMA / PAMMA) and top nav."""

import pathlib

import pytest

import test_noise_website as website

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LEKTOR_PROJECT = REPO_ROOT / "website" / "mjolnir-website.lektorproject"
STATUS = "https://www.hamma.dev/latest/{}_status.html"


@pytest.fixture(scope="module")
def server_pages():
    website._install_stubs()
    ns = {}
    exec(compile(website.MAIN_PY.read_text(), str(website.MAIN_PY), "exec"),  # noqa: S102
         ns)
    return ns["CONTENT_PAGES_SERVER"]


def _description(page):
    return page["blocks"]["overview"]["metadata"]["section_description"]


def test_top_nav_is_the_three_arrays():
    """Triggers and Old Plot Page are gone (PB, 2026-10-30)."""
    line = next(line for line in LEKTOR_PROJECT.read_text().splitlines()
                if line.startswith("nav_extralinks"))
    assert line == "nav_extralinks = HAMMA: /, AUMMA: /aumma/, PAMMA: /pamma/"


@pytest.mark.parametrize("page, array", [
    ("", "hamma"), ("aumma", "aumma"), ("pamma", "pamma")])
def test_each_array_page_links_its_status_page(server_pages, page, array):
    description = _description(server_pages[page])
    # Markdown link: the dashboard description field is type=markdown.
    assert "]({})".format(STATUS.format(array)) in description
    for other in {"hamma", "aumma", "pamma"} - {array}:
        assert STATUS.format(other) not in description
