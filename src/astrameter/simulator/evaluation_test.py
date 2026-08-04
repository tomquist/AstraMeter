"""Tests for the steering-evaluation harness."""

import socket

import pytest

from .evaluation import _reserve_udp_port


class TestReserveUdpPort:
    """The port handed to a scenario has to still be free when it is bound.

    Every scenario in the suite starts a CT002 listener, and the suite runs
    them in parallel across cores. Handing out a port number without holding
    it let two workers pick the same one, and the loser aborted the whole run
    with ``[Errno 98] Address already in use`` — a failure with nothing to do
    with the change under evaluation.
    """

    def test_port_is_held_against_other_binders(self):
        reservation = _reserve_udp_port()
        port = reservation.getsockname()[1]
        try:
            # The wildcard bind a real listener does, which is what a parallel
            # worker would attempt with the same number.
            with (
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as rival,
                pytest.raises(OSError),
            ):
                rival.bind(("0.0.0.0", port))
        finally:
            reservation.close()

    def test_port_is_bindable_once_released(self):
        reservation = _reserve_udp_port()
        port = reservation.getsockname()[1]
        reservation.close()
        # The handover the caller performs immediately before starting CT002.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.bind(("0.0.0.0", port))

    def test_concurrent_reservations_are_distinct(self):
        """Two live reservations cannot name the same port — the property the
        parallel workers actually depend on."""
        reservations = [_reserve_udp_port() for _ in range(16)]
        try:
            ports = [r.getsockname()[1] for r in reservations]
            assert len(set(ports)) == len(ports)
        finally:
            for r in reservations:
                r.close()
