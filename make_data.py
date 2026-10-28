"""生成 dwh_mini 的模拟源数据（一次性脚本，跑一次就不再需要）。

    ..\\.venv\\Scripts\\python.exe make_data.py

产出三份 CSV，模拟「电商业务库 + 商品维表」：

    data/ods_order.csv     订单表     约 340 单，跨 7 天
    data/ods_payment.csv   支付表     只覆盖已支付订单
    data/dim_product.csv   商品源表   11 行 = 10 个商品，其中 P002 在 9-04 改过价

## 两个设计选择，都不是随意的

**① P002 必须有两个版本。**
    这是 SCD2 的唯一考点。如果每个商品只有一行，拉链表就是纯装饰 ——
    谁都写得出「反正不改历史」的假实现。必须有一个商品在中间改过价，
    才能验证「9-04 之前的订单要关联到旧价格（299），之后关联新价格（269）」。

**② 日 GMV 的环比波动必须 < 25%。**
    因为「日环比突变 < 30%」是一条质量断言。如果造出来的数据本身就跳 50%，
    那断言第一天就报错 —— 实现的人会以为是自己写错了。
    **数据要满足业务常识，断言才有意义。** 所以脚本会自动挑一个波动合理的随机种子。
"""
import os
import random
from collections import Counter
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# ============================================================================
# 商品源表：10 个商品，P002 在 2026-09-04 改价（299 → 269）
# ============================================================================
PRODUCTS = [
    ("P001", "无线鼠标", "数码", 99.0,  "2026-08-01 00:00:00"),
    ("P002", "机械键盘", "数码", 299.0, "2026-08-01 00:00:00"),
    ("P003", "显示器",   "数码", 899.0, "2026-08-01 00:00:00"),
    ("P004", "保温杯",   "家居", 59.0,  "2026-08-01 00:00:00"),
    ("P005", "台灯",     "家居", 129.0, "2026-08-01 00:00:00"),
    ("P006", "跑步鞋",   "服饰", 399.0, "2026-08-01 00:00:00"),
    ("P007", "卫衣",     "服饰", 199.0, "2026-08-01 00:00:00"),
    ("P008", "咖啡豆",   "食品", 89.0,  "2026-08-01 00:00:00"),
    ("P009", "坚果礼盒", "食品", 149.0, "2026-08-01 00:00:00"),
    ("P010", "数据线",   "数码", 29.0,  "2026-08-01 00:00:00"),
    ("P002", "机械键盘", "数码", 269.0, "2026-09-04 00:00:00"),   # ← 改价
]

DAYS = 7
DAY0 = datetime(2026, 9, 1)


def price_at(pid, ts):
    """下单时该用哪个价格：取 update_time <= 下单时间 的最新一版。"""
    rows = [p for p in PRODUCTS if p[0] == pid and p[4] <= ts]
    return max(rows, key=lambda p: p[4])[3] if rows else None


def gen(seed):
    """按种子生成 (orders, payments)。同一 seed 结果完全一致。"""
    random.seed(seed)
    orders, payments = [], []
    oid = 0
    for d in range(DAYS):
        for _ in range(random.randint(45, 55)):        # 每天 45—55 单
            oid += 1
            pid = random.choice([p[0] for p in PRODUCTS[:10]])
            qty = random.choices([1, 1, 1, 2, 3], k=1)[0]
            ts = DAY0 + timedelta(days=d, hours=random.randint(9, 21),
                                  minutes=random.randint(0, 59),
                                  seconds=random.randint(0, 59))
            ts_s = ts.strftime("%Y-%m-%d %H:%M:%S")
            amount = round(price_at(pid, ts_s) * qty, 2)

            r = random.random()                        # 90% 已支付 / 6% 未支付 / 4% 取消
            status = "PAID" if r < 0.90 else ("UNPAID" if r < 0.96 else "CANCELLED")
            orders.append({"order_id": f"O{oid:05d}",
                           "user_id": f"U{random.randint(1000, 1999)}",
                           "product_id": pid, "quantity": qty, "amount": amount,
                           "order_status": status, "create_time": ts_s})

    pay_id = 0
    for o in orders:
        if o["order_status"] != "PAID":
            continue                                   # 未支付/取消没有支付记录
        pay_id += 1
        pay_ts = datetime.strptime(o["create_time"], "%Y-%m-%d %H:%M:%S") \
                 + timedelta(minutes=random.randint(1, 30))
        pstatus = "REFUND" if random.random() < 0.08 else "SUCCESS"   # 8% 退款
        payments.append({"pay_id": f"PAY{pay_id:05d}", "order_id": o["order_id"],
                         "pay_amount": o["amount"] if pstatus == "SUCCESS" else 0.0,
                         "pay_status": pstatus,
                         "pay_time": pay_ts.strftime("%Y-%m-%d %H:%M:%S")})
    return orders, payments


def daily_gmv(orders, payments):
    """每日 GMV = 当天订单里 SUCCESS 支付金额之和（按订单下单日归集）。"""
    o2d = {o["order_id"]: o["create_time"][:10] for o in orders}
    g = Counter()
    for p in payments:
        if p["pay_status"] == "SUCCESS":
            g[o2d[p["order_id"]]] += p["pay_amount"]
    return dict(sorted(g.items()))


def main():
    os.makedirs(DATA, exist_ok=True)

    # 挑一个「日环比最大波动 < 25%」的种子
    best = None
    for seed in range(20261026, 20261026 + 500):
        orders, payments = gen(seed)
        gmv = daily_gmv(orders, payments)
        days = sorted(gmv)
        mx = max(abs(gmv[days[i]] / gmv[days[i - 1]] - 1) for i in range(1, len(days)))
        if best is None or mx < best[1]:
            best = (seed, mx)
        if mx < 0.25:
            break
    seed, mx = best
    orders, payments = gen(seed)

    with open(os.path.join(DATA, "dim_product.csv"), "w", encoding="utf-8") as f:
        f.write("product_id,product_name,category,price,update_time\n")
        for p in PRODUCTS:
            f.write(",".join(str(x) for x in p) + "\n")

    cols_o = ("order_id", "user_id", "product_id", "quantity",
              "amount", "order_status", "create_time")
    with open(os.path.join(DATA, "ods_order.csv"), "w", encoding="utf-8") as f:
        f.write(",".join(cols_o) + "\n")
        for o in orders:
            f.write(",".join(str(o[c]) for c in cols_o) + "\n")

    cols_p = ("pay_id", "order_id", "pay_amount", "pay_status", "pay_time")
    with open(os.path.join(DATA, "ods_payment.csv"), "w", encoding="utf-8") as f:
        f.write(",".join(cols_p) + "\n")
        for p in payments:
            f.write(",".join(str(p[c]) for c in cols_p) + "\n")

    paid = sum(1 for o in orders if o["order_status"] == "PAID")
    success = sum(1 for p in payments if p["pay_status"] == "SUCCESS")
    print(f"seed = {seed}   日 GMV 最大环比 = {mx:+.1%}（要求 < 25%，否则断言会误报）")
    print(f"订单 {len(orders)} 条：PAID {paid} / UNPAID "
          f"{sum(1 for o in orders if o['order_status']=='UNPAID')} / CANCELLED "
          f"{sum(1 for o in orders if o['order_status']=='CANCELLED')}")
    print(f"支付 {len(payments)} 条：SUCCESS {success} / REFUND {len(payments)-success}")
    print(f"商品源表 {len(PRODUCTS)} 行 / {len({p[0] for p in PRODUCTS})} 个商品（P002 有 2 版）")
    print()
    print("每日订单数与 GMV：")
    cnt = Counter(o["create_time"][:10] for o in orders)
    gmv = daily_gmv(orders, payments)
    for d in sorted(cnt):
        print(f"  {d}  {cnt[d]:>3} 单   GMV {gmv[d]:>10,.2f}")


if __name__ == "__main__":
    main()
