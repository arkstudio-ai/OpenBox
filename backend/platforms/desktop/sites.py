"""Static catalogue of sites whose login state lives in the desktop browser.

Values were established by reconnaissance on a real desktop on 2026-09-08
(docs/A5_DESKTOP_LOGIN_STATE.md §3.10): which cookies mean "there is a
session", which JSON endpoint the site's own frontend calls that answers
differently when logged out, and where a nickname can be read from. A site
marked `recon_pending` is listed but never probed at level 2.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LightProbe:
    """One same-origin GET the site's own page already makes.

    `code_path` is a dotted path into the JSON body. A value in `ok_values`
    means logged in, one in `expired_values` means the session is gone, and
    anything else is `unknown` (never treated as expired).
    """

    url: str
    code_path: str = "status_code"
    ok_values: tuple = (0,)
    expired_values: tuple = ()
    nickname_path: str | None = None
    uid_path: str | None = None
    #: Extra dotted paths copied into probe_detail for display (店名 etc.).
    display_paths: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DesktopSite:
    key: str
    display: str
    group: str
    login_url: str
    home_url: str
    #: Cookie domains that belong to this site (leading dot = include subdomains).
    cookie_domains: tuple
    #: Cookie names that must all be present (and unexpired) for `cookie_ok`.
    session_cookies: tuple
    session_probe: LightProbe | None = None
    profile_probe: LightProbe | None = None
    #: Domains whose cookies "退出登录" removes.
    logout_domains: tuple = ()
    #: Sites that are known to react to automation: level-2 probes at most once
    #: a day and a failed one is never retried the same day.
    sensitive: bool = False
    #: Assumed "days without activity before the server drops the session";
    #: calibrated from probe history once the feature has run for a while.
    inactivity_ttl_days: int = 30
    recon_pending: bool = False

    @property
    def host(self) -> str:
        return self.home_url.split("/")[2]


SITES: tuple[DesktopSite, ...] = (
    DesktopSite(
        key="douyin_creator",
        display="抖音创作者中心",
        group="douyin",
        login_url="https://creator.douyin.com/",
        home_url="https://creator.douyin.com/creator-micro/home",
        cookie_domains=(".douyin.com",),
        # QR login verified 2026-09-11: these session cookies are present and
        # both creator endpoints return code 0 without passport_auth_status.
        # Requiring that auxiliary cookie prevents the server probe entirely.
        session_cookies=("sessionid", "sid_tt", "uid_tt"),
        session_probe=LightProbe(
            # The creator home page polls this itself every few tens of seconds.
            url="https://creator.douyin.com/aweme/v1/creator/user_message/unread_count/",
            code_path="status_code",
            ok_values=(0,),
            expired_values=(8,),
        ),
        profile_probe=LightProbe(
            url="https://creator.douyin.com/aweme/v1/creator/user/info/",
            code_path="status_code",
            ok_values=(0,),
            nickname_path="douyin_user_verify_info.nick_name",
            uid_path="douyin_user_verify_info.douyin_unique_id",
            display_paths={"followers": "douyin_user_verify_info.follower_count"},
        ),
        logout_domains=(".douyin.com", "creator.douyin.com"),
        sensitive=True,
        inactivity_ttl_days=30,
    ),
    DesktopSite(
        # 抖音热点宝. Established 2026-09-09 (docs/spikes/M0_AUTOPILOT_SPIKES_20260909.md
        # §A 续): it does NOT share the creator-centre session — opening it
        # redirects to an open.douyin.com OAuth page ("使用抖音账号登录 生活服务热点中心")
        # that the person scans once; the resulting cookies live on
        # .douhot.douyin.com and were observed to expire ~60 days out.
        key="douyin_hot",
        display="抖音热点宝",
        group="douyin",
        login_url="https://douhot.douyin.com/",
        home_url="https://douhot.douyin.com/analysis",
        cookie_domains=(".douhot.douyin.com", "douhot.douyin.com"),
        session_cookies=("sessionid_douhot", "sid_tt_douhot", "uid_tt_douhot"),
        session_probe=LightProbe(
            # The 我的数据 page calls this on load. Logged in: {code: 0, data: {...}}.
            url="https://douhot.douyin.com/douhot/v1/user/user_info",
            code_path="code",
            ok_values=(0,),
            nickname_path="data.nickname",
            uid_path="data.douyin_uid",
            display_paths={"followers": "data.follower_count"},
        ),
        logout_domains=(".douhot.douyin.com", "douhot.douyin.com"),
        sensitive=True,
        inactivity_ttl_days=60,
    ),
    DesktopSite(
        key="douyin_laike",
        display="抖音来客",
        group="douyin",
        login_url="https://life.douyin.com/",
        home_url="https://life.douyin.com/p/home",
        cookie_domains=(".life.douyin.com", ".douyin.com"),
        session_cookies=("sessionid_ls", "sid_tt_ls", "uid_tt_ls", "passport_auth_status_ls"),
        session_probe=LightProbe(
            url="https://life.douyin.com/life/gate/v1/user/login_info/",
            code_path="status_code",
            ok_values=(0,),
            expired_values=(4000100,),
            nickname_path="data.name",
            uid_path="data.user_id",
            display_paths={"role": "data.role_name"},
        ),
        profile_probe=LightProbe(
            url="https://life.douyin.com/life/gate/v1/account/detail",
            code_path="status_code",
            ok_values=(0,),
            expired_values=(4000100,),
            display_paths={"account_name": "data.account_name", "account_id": "data.account_id"},
        ),
        logout_domains=(".life.douyin.com", "life.douyin.com"),
        sensitive=True,
        inactivity_ttl_days=30,
    ),
    DesktopSite(
        key="meituan_merchant",
        display="美团经营宝（点评商户平台）",
        group="meituan",
        login_url="https://e.dianping.com/",
        home_url="https://e.dianping.com/app/merchant-platform",
        cookie_domains=(".dianping.com", ".meituan.com", "epassport.meituan.com"),
        # Long-lived tickets; the server-side session is what actually expires,
        # so level 1 is weak here and level 2 does the real work.
        session_cookies=("edper",),
        session_probe=LightProbe(
            url="https://e.dianping.com/merchant/portal/common/cityshop",
            code_path="error.code",
            ok_values=(None,),
            expired_values=(10008,),
            display_paths={"shop_id": "data.currentShopIdStr"},
        ),
        logout_domains=(".dianping.com", "e.dianping.com", ".meituan.com", "epassport.meituan.com"),
        sensitive=False,
        inactivity_ttl_days=7,
    ),
    DesktopSite(
        key="xiaohongshu_creator",
        display="小红书创作平台",
        group="xiaohongshu",
        login_url="https://creator.xiaohongshu.com/",
        home_url="https://creator.xiaohongshu.com/new/home",
        cookie_domains=(".xiaohongshu.com",),
        session_cookies=(),
        logout_domains=(".xiaohongshu.com",),
        sensitive=True,
        inactivity_ttl_days=14,
        recon_pending=True,
    ),
)

_BY_KEY = {site.key: site for site in SITES}


def get_site(key: str) -> DesktopSite:
    site = _BY_KEY.get(key)
    if site is None:
        raise KeyError(key)
    return site


def list_sites() -> list[DesktopSite]:
    return list(SITES)


def site_payload(site: DesktopSite) -> dict:
    """The subset the desktop-side script needs (no display strings)."""
    def probe(p: LightProbe | None) -> dict | None:
        if p is None:
            return None
        return {
            "url": p.url,
            "code_path": p.code_path,
            "nickname_path": p.nickname_path,
            "uid_path": p.uid_path,
            "display_paths": dict(p.display_paths),
        }

    return {
        "key": site.key,
        "host": site.host,
        "home_url": site.home_url,
        "login_url": site.login_url,
        "cookie_domains": list(site.cookie_domains),
        "session_cookies": list(site.session_cookies),
        "session_probe": probe(site.session_probe),
        "profile_probe": probe(site.profile_probe),
        "logout_domains": list(site.logout_domains),
    }


def public_site(site: DesktopSite) -> dict:
    return {
        "key": site.key,
        "display": site.display,
        "kind": "desktop",
        "group": site.group,
        "loginUrl": site.login_url,
        "sensitive": site.sensitive,
        "inactivityTtlDays": site.inactivity_ttl_days,
        "reconPending": site.recon_pending,
    }
