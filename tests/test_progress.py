import unittest

import monitor


class ProgressTests(unittest.TestCase):
    def test_application_columns_have_distinct_meanings(self):
        twse = ("索引,公司代號,公司簡稱,申請日期,董事長,股本,上市審議日期,董事會通過,上市契約,上市買賣日期\n"
                "1,1234,測試創意,1150101,,,1150201,1150301,1150401,1150501")
        tpex = ("申請日期,股票代號,公司名稱,董事長,股本,上櫃審議日期,董事會通過,上櫃契約,上櫃買賣日期\n"
                "20260101,1234,測試創意,,,20260201,20260301,20260401,20260501")
        for events in (monitor.parse_twse_apply_csv(twse), monitor.parse_tpex_apply(tpex)):
            self.assertCountEqual([e["stage"] for e in events],
                                  ["送件", "審議日期已公布", "董事會通過", "契約日期已公布", "掛牌日期已公布"])
            self.assertNotIn("申請創新板上市", [e["event_type"] for e in events])
        with self.assertRaises(ValueError):
            monitor.parse_twse_apply_csv(twse.replace("上市審議日期,董事會通過", "董事會通過,上市審議日期"))

    def test_news_requires_explicit_positive_outcome(self):
        header = "標題,連結,日期\n"
        items = ["審議未通過甲公司創新板上市案", "審議通過乙公司創新板上市案"]
        csv = header + "\n".join(f"{title},,1150101" for title in items)
        self.assertEqual([event["title"] for event in monitor.parse_tib_news_csv(csv)], items[1:])
        self.assertEqual(monitor.parse_tib_news_csv(header), [])
        with self.assertRaises(ValueError):
            monitor.parse_tib_news_csv("<html>無法下載</html>")
