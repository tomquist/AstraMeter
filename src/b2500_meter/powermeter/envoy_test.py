import unittest
from unittest.mock import patch, MagicMock
from b2500_meter.powermeter.envoy import Envoy, _find_measurement, obtain_token


SAMPLE_RESPONSE = {
    "production": [
        {
            "type": "inverters",
            "activeCount": 12,
            "readingTime": 1710500000,
            "wNow": 2500,
            "whLifetime": 12345678,
        }
    ],
    "consumption": [
        {
            "measurementType": "total-consumption",
            "wNow": 1200.5,
            "whLifetime": 9876543,
            "rmsVoltage": 240.0,
            "rmsCurrent": 5.0,
            "lines": [
                {"wNow": 400.0, "whLifetime": 3000000},
                {"wNow": 350.0, "whLifetime": 3200000},
                {"wNow": 450.5, "whLifetime": 3676543},
            ],
        },
        {
            "measurementType": "net-consumption",
            "wNow": -300.0,
            "whLifetime": 5000000,
            "rmsVoltage": 240.0,
            "rmsCurrent": 1.25,
            "lines": [
                {"wNow": -100.0, "whLifetime": 1500000},
                {"wNow": -80.0, "whLifetime": 1700000},
                {"wNow": -120.0, "whLifetime": 1800000},
            ],
        },
    ],
}


class TestFindMeasurement(unittest.TestCase):
    def test_find_by_measurement_type(self):
        result = _find_measurement(SAMPLE_RESPONSE["consumption"], "net-consumption")
        self.assertIsNotNone(result)
        self.assertEqual(result["wNow"], -300.0)

    def test_find_by_type(self):
        result = _find_measurement(SAMPLE_RESPONSE["production"], "inverters")
        self.assertIsNotNone(result)
        self.assertEqual(result["wNow"], 2500)

    def test_not_found(self):
        result = _find_measurement(SAMPLE_RESPONSE["consumption"], "nonexistent")
        self.assertIsNone(result)


class TestEnvoySinglePhase(unittest.TestCase):
    @patch("powermeter.envoy.requests.Session")
    def test_get_powermeter_watts_single_phase(self, mock_session_class):
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_RESPONSE
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response
        mock_session_class.return_value = mock_session

        envoy = Envoy(host="192.168.1.200", token="test-token", phases=1)
        result = envoy.get_powermeter_watts()

        self.assertEqual(result, [-300])
        mock_session.get.assert_called_once()
        call_args = mock_session.get.call_args
        self.assertIn("Bearer test-token", call_args[1]["headers"]["Authorization"])


class TestEnvoyThreePhase(unittest.TestCase):
    @patch("powermeter.envoy.requests.Session")
    def test_get_powermeter_watts_three_phase(self, mock_session_class):
        mock_response = MagicMock()
        mock_response.json.return_value = SAMPLE_RESPONSE
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response
        mock_session_class.return_value = mock_session

        envoy = Envoy(host="192.168.1.200", token="test-token", phases=3)
        result = envoy.get_powermeter_watts()

        self.assertEqual(result, [-100, -80, -120])


class TestEnvoyNoNetConsumption(unittest.TestCase):
    @patch("powermeter.envoy.requests.Session")
    def test_raises_when_only_total_consumption(self, mock_session_class):
        data = {
            "production": [],
            "consumption": [
                {
                    "measurementType": "total-consumption",
                    "wNow": 800.0,
                }
            ],
        }
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response
        mock_session_class.return_value = mock_session

        envoy = Envoy(host="192.168.1.200", token="test-token", phases=1)
        with self.assertRaises(ValueError):
            envoy.get_powermeter_watts()

    @patch("powermeter.envoy.requests.Session")
    def test_raises_when_no_consumption_data(self, mock_session_class):
        data = {"production": [], "consumption": []}
        mock_response = MagicMock()
        mock_response.json.return_value = data
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_session = MagicMock()
        mock_session.get.return_value = mock_response
        mock_session_class.return_value = mock_session

        envoy = Envoy(host="192.168.1.200", token="test-token", phases=1)
        with self.assertRaises(ValueError):
            envoy.get_powermeter_watts()


class TestEnvoyTokenRefreshOn401(unittest.TestCase):
    @patch("powermeter.envoy.obtain_token", return_value="new-token")
    @patch("powermeter.envoy.requests.Session")
    def test_refreshes_token_on_401(self, mock_session_class, mock_obtain):
        # First call returns 401, second returns 200
        resp_401 = MagicMock()
        resp_401.status_code = 401
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = SAMPLE_RESPONSE
        resp_200.raise_for_status = MagicMock()

        mock_session = MagicMock()
        mock_session.get.side_effect = [resp_401, resp_200]
        mock_session_class.return_value = mock_session

        envoy = Envoy(
            host="192.168.1.200",
            token="expired-token",
            phases=1,
            username="user@test.com",
            password="pass",
            serial="123456",
        )
        result = envoy.get_powermeter_watts()

        self.assertEqual(result, [-300])
        mock_obtain.assert_called_once_with("user@test.com", "pass", "123456")
        self.assertEqual(envoy.token, "new-token")


class TestEnvoyAutoObtainToken(unittest.TestCase):
    @patch("powermeter.envoy.obtain_token", return_value="fresh-token")
    @patch("powermeter.envoy.requests.Session")
    def test_obtains_token_when_none_provided(self, mock_session_class, mock_obtain):
        resp_200 = MagicMock()
        resp_200.status_code = 200
        resp_200.json.return_value = SAMPLE_RESPONSE
        resp_200.raise_for_status = MagicMock()

        mock_session = MagicMock()
        mock_session.get.return_value = resp_200
        mock_session_class.return_value = mock_session

        envoy = Envoy(
            host="192.168.1.200",
            token="",
            phases=1,
            username="user@test.com",
            password="pass",
            serial="123456",
        )
        result = envoy.get_powermeter_watts()

        self.assertEqual(result, [-300])
        mock_obtain.assert_called_once()
        self.assertEqual(envoy.token, "fresh-token")


class TestObtainToken(unittest.TestCase):
    @patch("powermeter.envoy.requests.post")
    def test_obtain_token_success(self, mock_post):
        login_resp = MagicMock()
        login_resp.json.return_value = {"session_id": "abc123"}
        login_resp.raise_for_status = MagicMock()

        token_resp = MagicMock()
        token_resp.text = "eyJhbGciOiJSUzI1NiJ9.test.token"
        token_resp.raise_for_status = MagicMock()

        mock_post.side_effect = [login_resp, token_resp]

        token = obtain_token("user@test.com", "pass", "123456")
        self.assertEqual(token, "eyJhbGciOiJSUzI1NiJ9.test.token")

    @patch("powermeter.envoy.requests.post")
    def test_obtain_token_login_fails(self, mock_post):
        login_resp = MagicMock()
        login_resp.json.return_value = {"message": "Invalid credentials"}
        login_resp.raise_for_status = MagicMock()

        mock_post.return_value = login_resp

        with self.assertRaises(ValueError) as ctx:
            obtain_token("user@test.com", "wrongpass", "123456")
        self.assertIn("no session_id", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
