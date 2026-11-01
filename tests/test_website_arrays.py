"""hamma.dev's per-array overview pages (HAMMA / AUMMA / PAMMA) and top nav."""

import pathlib

import pytest

import test_noise_website as website

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LEKTOR_PROJECT = REPO_ROOT / "website" / "mjolnir-website.lektorproject"
STATUS = "https://www.hamma.dev/latest/{}_status.html"


@pytest.fixture(scope="module")
def main_ns():
    website._install_stubs()
    ns = {}
    exec(compile(website.MAIN_PY.read_text(), str(website.MAIN_PY), "exec"),  # noqa: S102
         ns)
    return ns


@pytest.fixture(scope="module")
def server_pages(main_ns):
    return main_ns["CONTENT_PAGES_SERVER"]


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


ARRAYS = {
    "": ["hamma{}".format(n) for n in range(1, 10)],
    "aumma": ["hamma41", "hamma42", "hamma43"],
    # mj55 has not reported since 2023-04 and pamma_status.html leaves it
    # out too; off the grid by PB's decision (2026-11-01).
    "pamma": ["hamma{}".format(n) for n in (50, 51, 52, 53, 54, 56)],
}


def _cards(page):
    return page["blocks"]["overview"]["args"]["data_args"]["dashboard_plots"]


@pytest.mark.parametrize("page", sorted(ARRAYS))
def test_each_page_has_exactly_its_arrays_units(server_pages, page):
    assert sorted(_cards(server_pages[page])) == sorted(ARRAYS[page])


def test_every_unit_has_a_server_data_dir(main_ns):
    dirs = {str(d) for d in main_ns["UNIT_DIRS_SERVER"]}
    assert dirs == {u for units in ARRAYS.values() for u in units}


@pytest.mark.parametrize("page", sorted(ARRAYS))
def test_each_card_links_its_unit_page(server_pages, page):
    for unit, card in _cards(server_pages[page]).items():
        assert card["plot_metadata"]["card_url"] == "/" + unit
        assert card["plot_metadata"]["plot_title"] == unit.upper()


@pytest.mark.parametrize("page", sorted(ARRAYS))
def test_each_card_shows_every_gauge_in_its_own_cell(main_ns, server_pages,
                                                     page):
    keys = main_ns["OVERVIEW_SUBPLOT_KEYS"]
    assert "weblatency" not in keys, "site latency belongs in its own block"
    for unit, card in _cards(server_pages[page]).items():
        params = card["plot_params"]
        subplots = params["subplots"]
        assert sorted(subplots) == sorted(keys), unit
        cells = {(sp["subplot_row"], sp["subplot_column"])
                 for sp in subplots.values()}
        assert len(cells) == len(keys), "two gauges share a cell: " + unit
        assert all(r < params["subplot_rows"] and c < params["subplot_columns"]
                   for r, c in cells), unit


@pytest.mark.parametrize("page", sorted(ARRAYS))
def test_each_cards_source_latency_reads_its_own_unit(server_pages, page):
    for unit, card in _cards(server_pages[page]).items():
        code = card["plot_params"]["subplots"]["sourcelatency"][
            "plot_update_code"]
        assert "lastUpdateSources_overview.{})".format(unit) in code, unit


@pytest.mark.parametrize("page", sorted(ARRAYS))
def test_each_page_has_one_site_latency_block(server_pages, page):
    block = server_pages[page]["blocks"]["sitelatency"]
    plots = block["args"]["data_args"]["dashboard_plots"]
    assert list(plots) == ["sitelatency"]
    plot = plots["sitelatency"]
    assert plot["plot_metadata"]["plot_title"] == "Site Latency"
    code = plot["plot_params"]["plot_update_code"]
    assert "_sitelatency" in code and "_status" not in code


def test_a_grid_too_small_for_the_gauges_is_refused(main_ns):
    """itertools.product + zip silently dropped the gauges that did not
    fit; a 2x3 card would have lost two of the eight."""
    with pytest.raises(ValueError):
        main_ns["create_overview_dashboard"](["hamma1"], 2, 3)
