from __future__ import annotations

from pathlib import Path

from http.cookiejar import Cookie, CookieJar

from cookies import (
    append_ledger_row,
    cookie_header,
    cookies_from_jar,
    filter_dola_cookies,
    has_dola_auth,
    load_facebook_cookie_rows,
    missing_facebook_cookies,
    parse_cookie_header,
    write_profile_files,
)
from harvest_dola_cookies import (
    access_token_from_status_body,
    apply_facebook_cookies,
    assign_sticky_proxy,
    extract_html_redirect,
    facebook_login_url,
    facebook_oauth_dialog_url,
    make_session,
    mask_proxy_url,
    normalize_proxy_url,
    oauth_values_from_url,
    page_looks_like_checkpoint,
    page_looks_like_facebook_login,
    parse_html_forms,
    pick_continue_form,
    ProxyPool,
)


SAMPLE_HEADER = (
    "c_user=61590000000000; xs=8:TESTXSVALUE:2:1786149873:-1:-1; "
    "fr=test-fr-value; datr=test-datr-value"
)


def test_parse_cookie_header_accepts_semicolon_format() -> None:
    parsed = parse_cookie_header(SAMPLE_HEADER)
    assert parsed["c_user"] == "61590000000000"
    assert parsed["xs"].startswith("8:TESTXSVALUE")
    assert parsed["datr"] == "test-datr-value"


def test_parse_cookie_header_ignores_comments_and_cookie_prefix() -> None:
    parsed = parse_cookie_header("# ignore\nCookie: c_user=1; xs=abc\n")
    assert parsed == {"c_user": "1", "xs": "abc"}


def test_missing_facebook_cookies_requires_c_user_and_xs() -> None:
    assert missing_facebook_cookies({"c_user": "1"}) == ["xs"]
    assert missing_facebook_cookies({"c_user": "1", "xs": "secret"}) == []
    assert "sb" not in missing_facebook_cookies(parse_cookie_header(SAMPLE_HEADER))


def test_apply_facebook_cookies_keeps_minted_sb() -> None:
    session = make_session(5)
    session.cookies.set("sb", "minted-sb", domain=".facebook.com", path="/", secure=True)
    apply_facebook_cookies(session, {"c_user": "1", "xs": "secret"})
    assert session.cookies.get("sb") == "minted-sb"
    assert session.cookies.get("c_user") == "1"


def test_load_facebook_cookie_rows_reads_one_account_per_line() -> None:
    rows = load_facebook_cookie_rows("c_user=1; xs=a\nc_user=2; xs=b\n")
    assert [row["c_user"] for row in rows] == ["1", "2"]


def test_filter_dola_cookies_keeps_dola_domain_only() -> None:
    filtered = filter_dola_cookies(
        [
            {"name": "sessionid", "value": "secret", "domain": ".dola.com", "path": "/"},
            {"name": "sid_guard", "value": "guard", "domain": "www.dola.com", "path": "/"},
            {"name": "c_user", "value": "fb", "domain": ".facebook.com", "path": "/"},
            {"name": "empty", "value": "", "domain": ".dola.com", "path": "/"},
        ]
    )
    assert [item["name"] for item in filtered] == ["sessionid", "sid_guard"]
    assert cookie_header(filtered) == "sessionid=secret; sid_guard=guard"
    assert has_dola_auth(filtered) is True
    assert has_dola_auth({"ttwid": "public"}) is False
    assert has_dola_auth({"passport_csrf_token": "csrf"}) is False
    assert has_dola_auth({"odin_tt": "device"}) is False
    assert has_dola_auth({"uid_tt": "user"}) is True


def test_facebook_login_url_uses_dola_facebook_app() -> None:
    url = facebook_login_url()
    assert url.startswith("https://www.dola.com/passport/web/web_login/")
    assert "aid=495671" in url
    assert "platform=facebook" in url
    assert "action=login" in url


def test_write_profile_and_excel_ledger(tmp_path: Path) -> None:
    cookies = [{"name": "sessionid", "value": "secret", "domain": ".dola.com", "path": "/", "secure": True, "httpOnly": True}]
    paths = write_profile_files(tmp_path, facebook_c_user_id="61590000000000", cookies=cookies, stamp="20260919_120000")
    ledger = append_ledger_row(
        tmp_path,
        {
            "timestamp": "20260919_120000",
            "facebook_c_user": "61590000000000",
            "status": "ok",
            "dola_auth": "yes",
            "cookie_names": "sessionid",
            "cookie_header": "sessionid=secret",
            "json_path": str(paths["json"]),
            "error": "",
        },
    )
    assert "sessionid" in paths["json"].read_text(encoding="utf-8")
    assert paths["txt"].read_text(encoding="utf-8") == "sessionid=secret"
    csv_text = ledger["csv"].read_text(encoding="utf-8-sig")
    assert "61590000000000" in csv_text
    assert "sessionid=secret" in csv_text
    xls_text = ledger["xls"].read_text(encoding="utf-8")
    assert "Excel.Sheet" in xls_text
    assert "61590000000000" in xls_text


def test_facebook_oauth_dialog_url_uses_code_flow() -> None:
    url = facebook_oauth_dialog_url()
    assert url.startswith("https://www.facebook.com/v21.0/dialog/oauth?")
    assert "client_id=1819912395072617" in url
    assert "display=popup" in url
    assert "sdk=joey" in url
    assert "xd_arbiter" in url


def test_oauth_values_from_url_reads_query_and_fragment() -> None:
    assert oauth_values_from_url("https://www.dola.com/chat/?code=abc123")["code"] == "abc123"
    assert oauth_values_from_url("https://www.dola.com/chat/#access_token=tok")["access_token"] == "tok"


def test_continue_form_prefers_facebook_oauth_confirm() -> None:
    html = """
    <form action="/login.php" method="post"><input name="email" value="x"></form>
    <form action="/v21.0/dialog/oauth/confirm" method="post">
      <input type="hidden" name="fb_dtsg" value="token">
      <button name="__CONFIRM__" value="1">Continue as Test</button>
    </form>
    """
    form = pick_continue_form(parse_html_forms(html))
    assert form is not None
    assert form["inputs"]["fb_dtsg"] == "token"
    assert "__CONFIRM__" in form["inputs"]


def test_extract_html_redirect_finds_dola_callback() -> None:
    html = '<script>window.location.href="https://www.dola.com/passport/web/web_login_success?code=abc"</script>'
    assert "web_login_success" in extract_html_redirect(html, "https://www.facebook.com/dialog/oauth")


def test_facebook_login_and_checkpoint_detectors() -> None:
    assert page_looks_like_facebook_login("https://m.facebook.com/login.php", "<html>Log in to Facebook</html>")
    assert page_looks_like_checkpoint("https://www.facebook.com/checkpoint/", "confirm your identity")
    assert not page_looks_like_facebook_login("https://m.facebook.com/", "<html>News Feed</html>")


def test_access_token_from_status_body() -> None:
    token = access_token_from_status_body('waitForAll(null, {"authResponse":{"accessToken":"EAABtest"}})')
    assert token == "EAABtest"


def test_cookies_from_jar_keeps_dola_auth_only() -> None:
    jar = CookieJar()
    jar.set_cookie(
        Cookie(0, "sessionid", "secret", None, False, ".dola.com", True, True, "/", True, True, None, False, None, None, {})
    )
    jar.set_cookie(
        Cookie(0, "c_user", "fb", None, False, ".facebook.com", True, True, "/", True, True, None, False, None, None, {})
    )
    filtered = cookies_from_jar(jar)
    assert [item["name"] for item in filtered] == ["sessionid"]
    assert has_dola_auth(filtered) is True


def test_cookies_from_jar_unwraps_curl_cffi_wrapper() -> None:
    inner = CookieJar()
    inner.set_cookie(
        Cookie(0, "sid_guard", "guard", None, False, ".dola.com", True, True, "/", True, True, None, False, None, None, {})
    )

    class Wrapper:
        jar = inner

        def __iter__(self):
            return iter(["sid_guard"])

    filtered = cookies_from_jar(Wrapper())
    assert [item["name"] for item in filtered] == ["sid_guard"]


def test_normalize_proxy_url_formats() -> None:
    assert normalize_proxy_url("http://u:p@1.2.3.4:8080") == "http://u:p@1.2.3.4:8080"
    assert normalize_proxy_url("u:p@1.2.3.4:8080") == "http://u:p@1.2.3.4:8080"
    assert normalize_proxy_url("1.2.3.4:8080:u:p") == "http://u:p@1.2.3.4:8080"
    assert normalize_proxy_url("1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert mask_proxy_url("http://user:secret@1.2.3.4:8080") == "http://user:***@1.2.3.4:8080"


def test_proxy_pool_leases_unique_slots() -> None:
    pool = ProxyPool(["http://a:1@1.1.1.1:1", "http://b:2@2.2.2.2:2"])
    first, slot_a = pool.lease(0)
    second, slot_b = pool.lease(0)  # preferred busy → other slot
    assert {slot_a, slot_b} == {0, 1}
    assert first != second
    pool.release(slot_a)
    again, slot_c = pool.lease(0)
    assert slot_c == 0
    assert again == "http://a:1@1.1.1.1:1"
    assert assign_sticky_proxy(["p0", "p1", "p2"], 4) == "p1"
