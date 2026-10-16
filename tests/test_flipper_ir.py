import unittest

from common.flipper_ir import InfraredFileError, parse_flipper_ir


SAMPLE = """Filetype: IR signals file
Version: 1
#
name: Power_on
type: parsed
protocol: NEC
address: 10 00 00 00
command: 01 00 00 00
#
name: Input_aux
type: raw
frequency: 38000
duty_cycle: 0.330000
data: 9000 4500 560 560 560 1690 560
"""


class FlipperIrTests(unittest.TestCase):
    def test_parses_parsed_and_raw_signals(self):
        signals = parse_flipper_ir(SAMPLE)
        self.assertEqual([signal.name for signal in signals], ["Power_on", "Input_aux"])
        self.assertEqual(signals[0].protocol, "NEC")
        self.assertEqual(signals[1].frequency, 38000)
        self.assertEqual(signals[1].data[-1], 560)

    def test_rejects_wrong_file_type(self):
        with self.assertRaisesRegex(InfraredFileError, "Unsupported"):
            parse_flipper_ir(SAMPLE.replace("IR signals file", "Flipper SubGhz RAW File"))

    def test_rejects_invalid_raw_duration(self):
        with self.assertRaisesRegex(InfraredFileError, "positive"):
            parse_flipper_ir(SAMPLE.replace("9000 4500", "9000 -4500"))


if __name__ == "__main__":
    unittest.main()
