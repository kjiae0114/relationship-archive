"""index.html에서 기능이 통째로 빠지는 회귀를 막는 간단한 검사."""
from pathlib import Path

HTML = (Path(__file__).resolve().parents[1] / "app" / "index.html").read_text(encoding="utf-8")


def render_body() -> str:
    start = HTML.index("function render(){")
    depth = 0
    for pos in range(HTML.index("{", start), len(HTML)):
        if HTML[pos] == "{":
            depth += 1
        elif HTML[pos] == "}":
            depth -= 1
            if depth == 0:
                return HTML[start:pos + 1]
    raise AssertionError("render() 끝을 찾지 못함")


def test_render_fills_tag_view():
    # 11-02 지도 추가 때 태그 모아보기를 그리는 코드가 빠져 화면이 비어 있었다.
    body = render_body()
    for target in ("#tagCloud", "#tagResultTitle", "#tagResults"):
        assert f"$('{target}').innerHTML" in body or f"$('{target}').textContent" in body, target


def test_music_and_movie_menu_is_wired():
    assert 'data-view="culture"' in HTML and 'id="view-culture"' in HTML
    assert "music:'음악'" in HTML and "movie:'영화'" in HTML
    type_select = HTML[HTML.index('<select id="type"'):]
    type_select = type_select[:type_select.index("</select>")]
    for value in ("music", "movie"):
        assert type_select.count(f'<option value="{value}">') == 1, value
    assert "renderCultureView();" in render_body()
