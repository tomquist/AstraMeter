from .script import Script


async def test_script_get_powermeter_watts_integer():
    script = Script('echo "456"')
    assert await script.get_powermeter_watts_async() == [456]


async def test_script_get_powermeter_watts_float():
    script = Script('echo "456.7"')
    assert await script.get_powermeter_watts_async() == [456.7]
