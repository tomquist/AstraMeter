import unittest
from unittest.mock import patch, MagicMock
from powermeter import VZLogger


class TestVZLogger(unittest.TestCase):

    @patch("requests.Session.get")
    def test_vzlogger_get_powermeter_watts_total(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "version": "0.8.9",
            "generator": "vzlogger",
            "data": [
                { "uuid": "0", "last": 1776549041735, "interval": -1, "protocol": "sml", "tuples": [ [ 1776549041735, 100] ] },
                { "uuid": "1", "last": 1776549041735, "interval": -1, "protocol": "sml", "tuples": [ [ 1776549041735, 200] ] },
                { "uuid": "2", "last": 1776549041735, "interval": -1, "protocol": "sml", "tuples": [ [ 1776549041735, 300] ] },
                { "uuid": "3", "last": 1776549041735, "interval": -1, "protocol": "sml", "tuples": [ [ 1776549041735, 400] ] },
                { "uuid": "4", "last": 1776549041735, "interval": -1, "protocol": "sml", "tuples": [ [ 1776549041735, 500] ] },
                { "uuid": "5", "last": 1776549041735, "interval": -1, "protocol": "sml", "tuples": [ [ 1776549041735, 600] ] },
                { "uuid": "6", "last": 1776549041735, "interval": -1, "protocol": "sml", "tuples": [ [ 1776549041735, 700] ] },
                { "uuid": "7", "last": 1776549041735, "interval": -1, "protocol": "sml", "tuples": [ [ 1776549041735, 800] ] }
            ]
        }
        mock_get.return_value = mock_response

        vzlogger = VZLogger("192.168.1.9", "8088", "4")
        self.assertEqual(vzlogger.get_powermeter_watts(), [500, 0, 0])

        vzlogger = VZLogger("192.168.1.9", "8088", "5,6,7")
        self.assertEqual(vzlogger.get_powermeter_watts(), [600, 700, 800])

        vzlogger = VZLogger("192.168.1.9", "8088", "5, 6, 7")
        self.assertEqual(vzlogger.get_powermeter_watts(), [600, 700, 800])

if __name__ == "__main__":
    unittest.main()
