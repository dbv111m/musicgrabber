"""Regression guards for the zvu4no provider's current hostname."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zvu4no


def test_zvu4no_uses_current_site_hostname():
    assert zvu4no._BASE_URL == "https://zvu4it.org"


def test_zvu4no_download_hostname_is_recognised():
    assert zvu4no.is_zvu4no_url("https://data.zvu4it.org/download-track/example.mp3")
    assert not zvu4no.is_zvu4no_url("https://data.zvu4no.org/download-track/example.mp3")
    assert not zvu4no.is_zvu4no_url("https://zvu4it.org.example.com/download-track/example.mp3")
