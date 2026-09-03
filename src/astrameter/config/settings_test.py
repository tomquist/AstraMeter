import dataclasses

from astrameter.config.settings import DEFAULT_CT_UDP_PORT, CtSettings
from astrameter.ct002 import UDP_PORT


def test_default_ct_udp_port_matches_the_emulator() -> None:
    """The config layer cannot import the emulator, so pin the two together."""
    assert DEFAULT_CT_UDP_PORT == UDP_PORT
    assert CtSettings().udp_port == UDP_PORT


def test_ct_settings_defaults_match_the_balancer() -> None:
    """``main._balancer_config`` wires the balancer by field name, so a default
    that drifts here would silently re-tune every install."""
    from astrameter.ct002.balancer import BalancerConfig

    ct = CtSettings()
    mismatches = {
        f.name: (getattr(ct, f.name, "<missing>"), f.default)
        for f in dataclasses.fields(BalancerConfig)
        if getattr(ct, f.name, "<missing>") != f.default
    }
    assert mismatches == {}
