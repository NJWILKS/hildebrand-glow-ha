from custom_components.hildebrand_glow.config_flow import (
    HildebrandGlowConfigFlow,
    HildebrandGlowOptionsFlow,
)


def test_options_flow_factory_does_not_assign_read_only_config_entry() -> None:
    flow = HildebrandGlowConfigFlow.async_get_options_flow(object())

    assert isinstance(flow, HildebrandGlowOptionsFlow)
