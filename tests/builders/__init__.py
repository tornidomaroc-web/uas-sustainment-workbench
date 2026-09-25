"""Minimal encoders that write synthetic ULog and DataFlash files for tests.

They follow the published formats (https://docs.px4.io/main/en/dev_log/ulog_file_format.html;
ArduPilot AP_Logger LogStructure.h) closely enough for pyulog and pymavlink to read them, so
tests exercise the real parsers without any real, privacy-bearing flight log.
"""
