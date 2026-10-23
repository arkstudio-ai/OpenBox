# Assistant IP portraits

All 34 built-in portraits come from the user-supplied `IP_去背景_总.zip` (2026-10-23).
The IDs below are persistent account preferences: do not renumber them or reuse them for different characters.

Archive SHA-256: `6534857b59ba6422a91e18e0f21620ecccd1ab6c1715a8e7b7405ab423e2352f`.

Assets are 512 × 512 WebP (quality 88, alpha quality 100). Only fully transparent outer margins were trimmed; each complete character fits within 480 × 480, with 16 pixels of transparent padding. No background was added. Vite fingerprints the assets and the gallery loads images lazily.

Legacy preferences resolve as `orb → ip-01`, `sprout → ip-10`, `cat → ip-12`. Existing custom portraits stay available. These visual choices never change the assistant's identity or model prompts.

`backend/assistant/appearance.py` owns the female/male/animal categories and each character's default voice gender, including animals; the appearance API supplies this catalog to the picker. With the default Expert model, female and custom portraits use `longanqian_v3.1`, and male portraits use `longanlufeng`. A saved user voice takes priority. Model-only changes do not save an automatic voice as a user choice; existing saved voices are preserved conservatively. Appearance changes apply to the next call and refresh the voice settings immediately.

| Stable ID | Source PNG (under 透明背景/) | Display name (zh-CN) |
| --- | --- | --- |
| ip-01 | 图片 (1).png | 银发西装 |
| ip-02 | 图片 (2).png | 蓝帽厨师 |
| ip-03 | 图片 (3).png | 知性伙伴 |
| ip-04 | 图片.png | 牛仔伙伴 |
| ip-05 | 图片79 (1).png | 甜心小厨 |
| ip-06 | 图片节点 1 (1).png | 制服伙伴 |
| ip-07 | 图片节点 1 (62).png | 草莓甜点师 |
| ip-08 | 图片节点 10 (16).png | 披萨仓鼠 |
| ip-09 | 图片节点 11 (1).png | 织彩少女 |
| ip-10 | 图片节点 14 (2).png | 小羊老师 |
| ip-11 | 图片节点 15 (3).png | 咖啡橘猫 |
| ip-12 | 图片节点 16 (6).png | 领结狸猫 |
| ip-13 | 图片节点 18 (2).png | 长颈鹿医生 |
| ip-14 | 图片节点 19 (6).png | 工程师小熊猫 |
| ip-15 | 图片节点 2 (1).png | 阳光小厨 |
| ip-16 | 图片节点 2 (43).png | 粉帽烘焙师 |
| ip-17 | 图片节点 2(3).png | 古风白衣 |
| ip-18 | 图片节点 3 (41).png | 白帽主厨 |
| ip-19 | 图片节点 3.png | 青衫少女 |
| ip-20 | 图片节点 4 (2).png | 书香少女 |
| ip-21 | 图片节点 4 (21).png | 蛋糕橘猫 |
| ip-22 | 图片节点 4(1).png | 红衣侠客 |
| ip-23 | 图片节点 4.png | 优雅伙伴 |
| ip-24 | 图片节点 5 (17).png | 冰淇淋小厨 |
| ip-25 | 图片节点 5(1).png | 花冠少女 |
| ip-26 | 图片节点 5.png | 书香少年 |
| ip-27 | 图片节点 6 (13).png | 丸子头伙伴 |
| ip-28 | 图片节点 6(1).png | 珠翠少女 |
| ip-29 | 图片节点 7 (23).png | 邮差金毛 |
| ip-30 | 图片节点 7.png | 贝雷帽画家 |
| ip-31 | 图片节点 8 (10).png | 元气橘猫 |
| ip-32 | 图片节点 8(1).png | 银饰少女 |
| ip-33 | 图片节点 9 (11).png | 冰淇淋猫 |
| ip-34 | 图片节点 9 (2).png | 制服少女 |
