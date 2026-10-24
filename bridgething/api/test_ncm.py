import unittest

from api.ncm import (
    DHCLIENT_HOOK,
    NM_PROFILE,
    candidate_gateways,
    cdc_interfaces,
    local_ncm_addresses,
    nmcli_add_args,
    nmcli_modify_args,
)


class NcmTests(unittest.TestCase):
    def test_profile_does_not_take_the_default_route(self) -> None:
        self.assertIn("match-device=driver:cdc_ncm", NM_PROFILE)
        self.assertIn("never-default=true", NM_PROFILE)
        self.assertIn("ignore-auto-dns=true", NM_PROFILE)
        self.assertNotIn("method=shared", NM_PROFILE)
        args = nmcli_add_args()
        self.assertIn("cdc_ncm", args)
        self.assertIn("ipv4.never-default", args)
        self.assertEqual(args[args.index("ipv4.never-default") + 1], "yes")
        modified = nmcli_modify_args()
        self.assertIn("ipv4.never-default", modified)
        self.assertEqual(modified[modified.index("ipv4.ignore-auto-dns") + 1], "yes")

    def test_dhcp_hook_drops_router_and_dns(self) -> None:
        self.assertIn('new_routers=""', DHCLIENT_HOOK)
        self.assertIn('new_domain_name_servers=""', DHCLIENT_HOOK)

    def test_only_cdc_ncm_interfaces_are_selected(self) -> None:
        found = cdc_interfaces(
            [("eth0", "r8152"), ("enp0s1", "cdc_ncm"), ("uncm0", None), ("wlan0", None)]
        )
        self.assertEqual(found, ["enp0s1", "uncm0"])

    def test_scan_skips_the_local_address_and_other_subnets(self) -> None:
        addresses = local_ncm_addresses("inet 10.42.1.9/29 inet 192.168.1.20/24 inet 10.42.0.4/24")
        self.assertEqual(addresses, [("10.42.1.9", 29)])
        self.assertEqual(
            candidate_gateways(addresses),
            ["bridgething.local", "10.42.1.10", "10.42.1.11", "10.42.1.12", "10.42.1.13", "10.42.1.14"],
        )

    def test_wide_prefix_is_narrowed_to_the_device_pool(self) -> None:
        hosts = candidate_gateways([("10.42.1.9", 24)])
        self.assertNotIn("10.42.1.9", hosts)
        self.assertIn("10.42.1.10", hosts)
        self.assertNotIn("10.42.1.20", hosts)
