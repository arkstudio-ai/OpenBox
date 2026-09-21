"""Deterministic, synthetic v1 evaluation inputs; no provider or network calls."""
from __future__ import annotations

import json
from pathlib import Path


def cases():
    rows = []
    for i in range(1, 6):
        # Three sources deliberately disagree; the dated correction wins.
        a, b, c = 90 + i * 3, 80 + i * 2, 100 + i
        target, demand = 500 + i * 40, 6 + i
        prompt = f"""为虚构项目 R{i} 写一份基于以下三份给定来源的采购调研结论。不访问外网，不假设未给数据。
[S1 2026-09-01 报价表] A单价{a}，B单价{b}，C单价{c}；均无税运费。
[S2 2026-09-02 交期表] A可在5天内交{demand}件，B可在4天内交{demand}件，C可在3天内交{demand}件。A不满足必须的防水要求，B和C满足。预算{target}元。
[S3 2026-09-03 勘误] B的防水认证已失效，不能算满足；C现价改为{c-15}元且可分批采购；预算增加到{(c-15)*demand}元。其余记录未变。
要求按最新有效记录选择满足防水、5天内交付全部{demand}件且不超预算的方案；计算总价、预算余额、相对旧价节省额；逐项引用来源，解释为什么另外两家不合格，不把旧记录当最新。
最后输出一个JSON对象，键为 supplier、quantity、total、remaining、saving、sources（覆盖所用来源ID），并附250字以内解释。"""
        rows.append({"id": f"research-{i}", "category": "research", "prompt": prompt,
            "expected": {"supplier": "C", "quantity": demand, "total": (c-15)*demand,
                "remaining": 0, "saving": 15*demand},
            "review": ["来源引用对应具体结论", "勘误取代旧值", "解释排除A/B", "结论和限制清晰"]})
    for i in range(1, 6):
        price, unit, sold, returned, fixed = 80+5*i, 41+2*i, 100+10*i, 2*i, 1800+150*i
        net, margin = sold-returned, price-unit
        revenue, contribution = net*price, net*margin
        prompt = f"""分析虚构门店 D{i}，写含公式、退款处理与盈亏解释的报告。
当月售出{sold}件，单价{price}元；其中{returned}件全额退款且完好退库，所以这些退货不计收入也不计变动成本。
单位变动成本{unit}元，月固定成本{fixed}元。另有上月已售货款回收500元，明确不算本月收入。
计算本月净销量、营业收入、贡献毛利、经营利润、整数盈亏平衡净销量；验证前一个整数销量不能达到盈亏平衡。不要混淆现金回收与收入。
最后输出JSON对象 net_units、revenue、contribution、profit、break_even，并附300字以内分析。"""
        rows.append({"id": f"data-{i}", "category": "data", "prompt": prompt,
            "expected": {"net_units": net, "revenue": revenue, "contribution": contribution,
                "profit": contribution-fixed, "break_even": (fixed+margin-1)//margin},
            "review": ["收入和现金分开", "退款成本处理正确", "公式可复算", "验证盈亏边界"]})
    snippets = [
        ("def ceil_div(a, b):\n    return a // b", "非负整数a、正整数b的向上整除",
            "[[0,3],[1,3],[3,3],[4,3],[10,4]]", [0,1,1,2,3], "整除有余数时少一"),
        ("def dedupe(xs):\n    return list(set(xs))", "稳定去重，保留元素第一次出现的顺序，输入整数列表",
            "[[3,1,3,2],[2,2],[],[5,4,5,4,3],[1]]", [[3,1,2],[2],[],[5,4,3],[1]], "集合不保留契约要求的顺序"),
        ("def moving_sum(xs, n):\n    return [sum(xs[i:i+n]) for i in range(len(xs)-n)]", "全部长度n的连续窗口之和，n正整数，输入不足n项时返回空",
            "[[[1,2,3],2],[[5],1],[[],1],[[1,2],3],[[2,4,6,8],2]]", [[3,5],[5],[],[],[6,10,14]], "最后一个合法窗口被遗漏"),
        ("def median(xs):\n    ys=sorted(xs)\n    return ys[len(ys)//2]", "非空数值列表的中位数，偶数项取中间两项的算术平均",
            "[[3,1,2],[1,4],[9],[4,1,3,2],[10,0,20,30]]", [2,2.5,9,2.5,15], "偶数长度只取一个中间数"),
        ("def intervals_overlap(a, b):\n    return max(a[0],b[0]) <= min(a[1],b[1])", "半开区间[start,end)是否存在正长度交集，零长度区间为空",
            "[[[0,2],[2,4]],[[0,3],[2,4]],[[1,1],[0,2]],[[0,5],[1,2]],[[5,8],[0,4]]]", [False,True,False,True,False], "边界相接和空区间被误判重叠"),
    ]
    for i,(code,contract,inputs,outputs,bug) in enumerate(snippets,1):
        rows.append({"id": f"code-{i}", "category": "code", "prompt": f"""审查并修正以下纯Python函数。契约：{contract}。
```python
{code}
```
给出最小修正后的完整函数（保留函数名），解释缺陷和复杂度，并逐个计算测试输入{inputs}的正确返回值（多参数用每个内层数组展开）。不得读写文件或调用外部服务。
最后输出JSON对象：results（按输入顺序的数组）、fixed_code（完整Python源码字符串）、reason（缺陷说明）。""",
            "expected": {"results": outputs}, "reference_bug": bug,
            "review": ["修正代码遵守契约", "覆盖给定边界条件", "缺陷定位准确", "复杂度分析准确"]})
    subjects = ["社区图书角", "手工陶艺体验", "旧衣修补工作坊", "城市步行观察", "阳台种植入门"]
    for i,subject in enumerate(subjects,1):
        rows.append({"id": f"content-{i}", "category": "content", "prompt": f"""为虚构账号“{subject}”设计一支30秒短视频，包含选题研究、脚本、分镜和独立复核。
唯一可用事实：[B1]活动周六14:00开始，90分钟，最多8人，费用{50+i*10}元/人；[B2]适合零基础，报名截止周五18:00，不承诺包会或效果；[B3]画面素材仅能使用桌面工具和双手操作，不拍人脸、儿童或品牌logo。
先给3个明显不同的选题并说明依据，从中选1个；写旁白和恰好6镜头分镜，每镜头5秒，画面与旁白对应；结尾CTA必须提到报名截止时间。做一次逐条事实/时长/画面约束审查。不要编造优惠、认证、效果或客户评价。
最终输出JSON对象 topic_count、shot_count、duration_seconds、price、deadline、topics（3项）、script、shots（6项）、review（审查结果）。""",
            "expected": {"topic_count": 3, "shot_count": 6, "duration_seconds": 30,
                "price": 50+i*10, "deadline": "周五18:00"},
            "review": ["三个选题有区分且引用事实", "分镜可执行且满足画面约束", "旁白时长与镜头对应", "复核发现或明确排除无依据承诺"]})
    for i in range(1,6):
        rows.append({"id": f"material-{i}", "category": "material", "requires_verified_media": True,
            "prompt": f"为虚构活动制作1张方形插画素材：{subjects[i-1]}，桌面物件与双手构图，柔和日光，无文字、logo、人脸。输出实际生成的图片文件，说明构图并由另一角色检查约束。总费用必须符合该轮已确认的媒体预授权。",
            "expected": {}, "review": ["实际生成的可打开文件", "符合主题与画面简报", "无未请求文字与logo", "构图清晰可用"]})
    return rows


if __name__ == "__main__":
    destination = Path(__file__).resolve().parents[2] / "docs/evaluations/agent-team-v1/cases.json"
    destination.write_text(json.dumps({"version": 1, "cases": cases()}, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {len(cases())} deterministic cases to {destination}")
