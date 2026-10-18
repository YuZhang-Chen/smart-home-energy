"""User-visible defaults shared by the assistant and its deterministic tools."""
from smart_home_energy.domain import number

DEFAULTS = {"target_c": 27.0, "duration_minutes": 60, "minimum_c": 26.0, "maximum_c": 28.0,
            "washer_hours": 24, "quiet_start": 23 * 60, "quiet_end": 7 * 60}


def preferences(repo):
    return {**DEFAULTS, **repo.meta("assistant_preferences", {})}


def save_preferences(repo, values):
    if set(values) != set(DEFAULTS):
        raise ValueError("偏好設定欄位不完整")
    for key in ("target_c", "minimum_c", "maximum_c"):
        number(values[key], "溫度", 16)
    if not 16 <= values["minimum_c"] <= values["target_c"] <= values["maximum_c"] <= 30 or values["minimum_c"] == values["maximum_c"]:
        raise ValueError("目標溫度必須在舒適範圍內，範圍介於 16–30°C")
    for key, low, high in (("duration_minutes", 15, 1440), ("washer_hours", 1, 24), ("quiet_start", 0, 1439), ("quiet_end", 0, 1439)):
        if type(values[key]) is not int or not low <= values[key] <= high:
            raise ValueError("時間偏好超出允許範圍")
    if values["quiet_start"] == values["quiet_end"]:
        raise ValueError("安靜時段起訖不能相同")
    repo.set_meta("assistant_preferences", values)
