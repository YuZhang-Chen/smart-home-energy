import math
import unittest

from smart_home_energy.simulator import AirConditioner, integrate_energy


class SimulatorTests(unittest.TestCase):
    def test_defaults_and_initial_power(self) -> None:
        state = AirConditioner()
        self.assertEqual(state.temperature_c, 30)
        self.assertEqual(state.power_w, 1000)
        self.assertEqual(state.energy_kwh, 0)

    def test_temperature_converges_without_overshooting(self) -> None:
        state = AirConditioner()
        for _ in range(3000):
            previous = state
            state = state.step(2)
            self.assertGreaterEqual(state.temperature_c, 27)
            self.assertLessEqual(state.temperature_c, previous.temperature_c)
            self.assertGreater(state.energy_kwh, previous.energy_kwh)
        self.assertAlmostEqual(state.temperature_c, 27, places=3)

    def test_step_leaves_original_state_unchanged(self) -> None:
        initial = AirConditioner()
        updated = initial.step(2)
        self.assertEqual(initial.temperature_c, 30)
        self.assertEqual(initial.energy_kwh, 0)
        self.assertGreater(updated.energy_kwh, 0)

    def test_power_is_clamped(self) -> None:
        self.assertEqual(AirConditioner(temperature_c=50).power_w, 1200)
        self.assertEqual(AirConditioner(temperature_c=20).power_w, 100)

    def test_one_kw_for_one_hour(self) -> None:
        self.assertEqual(integrate_energy(1000, 3600), 1)

    def test_irregular_intervals_use_previous_power(self) -> None:
        state = AirConditioner()
        expected = 0.0
        for seconds in (0, 0.2, 2, 7.5, 1.1, 3600):
            expected += state.power_w * seconds / 3_600_000
            state = state.step(seconds)
            self.assertAlmostEqual(state.energy_kwh, expected, places=14)

    def test_invalid_time_is_rejected(self) -> None:
        for seconds in (-1, math.inf, math.nan):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                AirConditioner().step(seconds)


if __name__ == "__main__":
    unittest.main()
