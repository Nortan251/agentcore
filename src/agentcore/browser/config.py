"""Explicit host-only browser configuration; no model launch options."""

from dataclasses import dataclass
import math

from agentcore.errors import ConfigError


@dataclass(frozen=True)
class BrowserConfig:
    enabled: bool = False
    operation_timeout_seconds: float = 15.0
    max_text_chars: int = 8000
    max_links: int = 50
    max_url_chars: int = 2048
    max_title_chars: int = 256
    max_link_text_chars: int = 256
    max_dom_nodes: int = 10000
    max_body_bytes: int = 262144
    dns_timeout_seconds: float = 5.0
    max_redirects: int = 3

    def validate(self) -> None:
        if type(self.enabled) is not bool:
            raise ConfigError("browser.enabled must be a boolean")
        value = self.operation_timeout_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 60:
            raise ConfigError("browser operation timeout must be between 1 and 60 seconds")
        value = self.dns_timeout_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or not .1 <= value <= 5:
            raise ConfigError("browser DNS timeout must be between .1 and 5 seconds")
        if type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 3:
            raise ConfigError("browser redirect count must be between 0 and 3")
        for name, ceiling in (("max_text_chars", 8000), ("max_links", 100),
                              ("max_url_chars", 2048), ("max_title_chars", 256),
                              ("max_link_text_chars", 256), ("max_dom_nodes", 10000),
                              ("max_body_bytes", 262144)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= ceiling:
                raise ConfigError("browser bound is invalid", field=name)
