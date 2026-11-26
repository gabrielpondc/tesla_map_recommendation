import unittest
from unittest.mock import Mock

from app.baidu import BaiduWebClient
from app.models import is_tesla_supercharger_name


class BaiduWebClientTest(unittest.TestCase):
    def test_only_superchargers_receive_tesla_supercharger_type(self):
        self.assertTrue(is_tesla_supercharger_name("特斯拉超级充电站（义乌吾悦广场）"))
        self.assertTrue(is_tesla_supercharger_name("Tesla Supercharger Shanghai"))
        self.assertFalse(is_tesla_supercharger_name("特斯拉目的地充电站"))

    def test_server_suggestions_keep_rank_and_convert_for_amap(self):
        client = BaiduWebClient(key="test")
        client._get = Mock(return_value={
            "result": [{
                "name": "紫荆苑",
                "address": "淮安市-清江浦区-深圳路30号",
                "district": "清江浦区",
                "location": {"lng": 119.082368, "lat": 33.598977},
            }],
        })
        baidu = client.suggest("淮安紫", map_provider="baidu")
        amap = client.suggest("淮安紫", map_provider="amap")
        self.assertEqual(baidu[0].name, "紫荆苑")
        self.assertIn("淮安市", baidu[0].address)
        self.assertNotAlmostEqual(baidu[0].lng, amap[0].lng, places=4)
        self.assertNotAlmostEqual(baidu[0].lat, amap[0].lat, places=4)


if __name__ == "__main__":
    unittest.main()
