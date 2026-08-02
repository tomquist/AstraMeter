from astrameter.config.settings import DEFAULT_CT_UDP_PORT, CtSettings
from astrameter.ct002 import UDP_PORT


def test_default_ct_udp_port_matches_the_emulator():
    """The config layer cannot import the emulator, so pin the two together."""
    assert DEFAULT_CT_UDP_PORT == UDP_PORT
    assert CtSettings().udp_port == UDP_PORT
