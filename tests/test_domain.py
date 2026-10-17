import unittest
from datetime import datetime, timedelta
from smart_home_energy.domain import (
    TAIPEI, LogicalClock, TaskConstraints, Tariff, WashProgram, encode, decode,
    integrate, make_plan, find_schedule, validate_plan, ApprovalEnvelope,
)


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 10, 17, 21, 30, tzinfo=TAIPEI)
        self.program, self.tariff = WashProgram(), Tariff()

    def test_large_step_and_boundary_cost(self):
        energy, cost, stage, power = integrate(self.program, self.start, 9000, self.tariff)
        self.assertEqual(self.program.seconds, 3600)
        self.assertAlmostEqual(energy, .225)
        self.assertAlmostEqual(cost, .1 * 6 + .125 * 2.5)
        self.assertEqual((stage, power), ("完成", 0))
        self.assertAlmostEqual(integrate(self.program, self.start, 1800, self.tariff)[0], .1)

    def test_cross_midnight_quiet_and_no_relaxation(self):
        c = TaskConstraints(self.start, self.start + timedelta(hours=12))
        plan = find_schedule(c, self.program, self.tariff, self.start, "clock")
        self.assertEqual(plan.start.astimezone(TAIPEI).hour, 22)
        self.assertEqual(plan.end.astimezone(TAIPEI).hour, 23)
        impossible = TaskConstraints(self.start + timedelta(hours=2), self.start + timedelta(hours=8))
        with self.assertRaisesRegex(ValueError, "沒有合法候選"):
            find_schedule(impossible, self.program, self.tariff, self.start, "clock")
        overnight = TaskConstraints(self.start, self.start + timedelta(hours=5), None, None)
        p = find_schedule(overnight, self.program, self.tariff, self.start, "clock")
        self.assertEqual(p.start.astimezone(TAIPEI).hour, 22)

    def test_approval_and_roundtrip(self):
        p = make_plan(self.start, self.program, self.tariff, "clock")
        self.assertEqual(decode(type(p), encode(p)), p)
        c = TaskConstraints(self.start, self.start + timedelta(hours=2), None, None)
        a = ApprovalEnvelope(c, self.program, self.tariff, "clock", p.cost)
        validate_plan(p, c, self.start, "clock", a)
        with self.assertRaises(ValueError):
            validate_plan(p, c, self.start + timedelta(seconds=1), "clock", a)
        with self.assertRaises(ValueError):
            validate_plan(p, c, self.start, "other", a)

    def test_acceleration_same_physics(self):
        clock = LogicalClock.create(60, self.start)
        end = clock.now(self.start + timedelta(seconds=60))
        self.assertEqual((end - self.start).total_seconds(), 3600)
        self.assertAlmostEqual(integrate(self.program, self.start, 3600, self.tariff)[0], .225)


if __name__ == "__main__":
    unittest.main()
