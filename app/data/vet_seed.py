"""兽医临床诊断关键词库 + 回访模板预置数据。

参考资料：
  - 《兽医内科学》（Ettinger & Feldman, Textbook of Veterinary Internal Medicine）
  - WSAVA Global Vaccination / Pain Council 指南
  - BSAVA Manuals 系列（消化/皮肤/呼吸/泌尿等专科）
  - 中华人民共和国《兽医临床诊疗规范》行业标准
  - ACVIM Consensus Statements（CKD / DM / IBD / CHF 等）

数据用于：
  1. 病例 diagnosis 字段 autocomplete 推荐
  2. 诊后回访模板的关键词匹配（自动衍生多轮回访）

允许后台增删改，is_builtin=True 的内置项不可删（防止误操作）。
"""

# ═══════════════════════════════════════════════════════════════
# 系统分类
# ═══════════════════════════════════════════════════════════════
SYSTEMS = {
    "general":     "通用门诊",
    "gi":          "消化系统",
    "respiratory": "呼吸系统",
    "skin":        "皮肤系统",
    "dental":      "口腔牙科",
    "ophthalmic":  "眼科",
    "urinary":     "泌尿系统",
    "renal":       "肾脏内科",
    "cardio":      "心血管",
    "neuro":       "神经系统",
    "endocrine":   "内分泌",
    "hemato":      "血液/免疫",
    "oncology":    "肿瘤",
    "ortho":       "骨科",
    "reproduction": "生殖/产科",
    "infectious":  "传染病",
    "surgical":    "手术后",
}

# ═══════════════════════════════════════════════════════════════
# 疾病字典（用于 diagnosis autocomplete）
# 每条：(name, system, aliases_csv, severity, species)
# severity: mild / moderate / severe / chronic
# species:  cat / dog / both
# ═══════════════════════════════════════════════════════════════
DISEASES = [
    # ── 消化系统 ──────────────────────────────────
    ("急性胃肠炎",       "gi", "AGE,急性胃肠炎,胃肠型,呕吐,干呕,反胃,没胃口,食欲不振", "moderate", "both"),
    ("慢性胃肠炎",       "gi", "慢性胃肠炎,慢性肠炎",                  "chronic",  "both"),
    ("炎症性肠病",       "gi", "IBD,炎性肠病,Inflammatory Bowel Disease", "chronic", "both"),
    ("急性胰腺炎",       "gi", "急性胰腺炎,胰腺炎",                    "severe",   "both"),
    ("慢性胰腺炎",       "gi", "慢性胰腺炎",                          "chronic",  "both"),
    ("胃溃疡",          "gi", "胃溃疡,Gastric Ulcer",                 "moderate", "both"),
    ("胃扭转",          "gi", "GDV,胃扩张扭转,胃膨胀",               "severe",   "dog"),
    ("肠套叠",          "gi", "肠套叠,Intussusception",              "severe",   "both"),
    ("肠梗阻",          "gi", "肠梗阻,肠阻塞,异物梗阻",              "severe",   "both"),
    ("胃肠异物",        "gi", "异物,异物梗阻,foreign body",          "severe",   "both"),
    ("巨结肠",          "gi", "巨结肠,Megacolon",                     "chronic",  "cat"),
    ("便秘",            "gi", "便秘,Constipation",                    "mild",     "both"),
    ("急性腹泻",        "gi", "急性腹泻,腹泻,拉稀,拉肚子,水样便,软便,Diarrhea", "mild", "both"),
    ("出血性肠炎",      "gi", "HGE,AHDS,出血性肠胃炎",                "severe",   "dog"),
    ("嗜酸性肠炎",      "gi", "嗜酸性肠炎,EE",                        "chronic",  "both"),
    ("蛋白丢失性肠病",  "gi", "PLE,Protein-Losing Enteropathy",       "chronic",  "both"),
    ("肝炎",            "gi", "肝炎,Hepatitis",                       "moderate", "both"),
    ("胆管炎",          "gi", "胆管炎,Cholangitis",                   "moderate", "both"),
    ("胆囊黏液囊肿",    "gi", "胆囊黏液囊肿,GBM",                    "severe",   "dog"),
    ("脂肪肝",          "gi", "脂肪肝,肝脂沉积,Hepatic Lipidosis",   "severe",   "cat"),
    ("肝胆综合征",      "gi", "三联炎,Triaditis",                    "chronic",  "cat"),
    ("门静脉短路",      "gi", "门静脉短路,PSS,Portosystemic Shunt",  "chronic",  "both"),
    ("食物过敏",        "gi", "食物过敏,Food Allergy",                "chronic",  "both"),
    ("食物不耐受",      "gi", "食物不耐受,Food Intolerance",          "mild",     "both"),
    ("蛔虫感染",        "gi", "蛔虫,Toxocara",                       "mild",     "both"),
    ("绦虫感染",        "gi", "绦虫,Dipylidium",                     "mild",     "both"),
    ("钩虫感染",        "gi", "钩虫,Ancylostoma",                    "moderate", "both"),
    ("球虫感染",        "gi", "球虫,Coccidia,Isospora",              "mild",     "both"),
    ("贾第虫感染",      "gi", "贾第鞭毛虫,贾第虫,Giardia",           "moderate", "both"),
    ("弓形虫感染",      "gi", "弓形虫,Toxoplasma",                   "moderate", "both"),
    ("猫冠状病毒肠炎",  "gi", "猫冠状,FCoV,FECV,冠状病毒性肠炎",     "moderate", "cat"),
    ("猫传染性腹膜炎",  "gi", "FIP,猫传腹,传染性腹膜炎",             "severe",   "cat"),
    ("犬细小病毒",      "gi", "犬细小,CPV,Parvo,细小病毒肠炎",       "severe",   "dog"),
    ("犬冠状病毒",      "gi", "犬冠状,CCV",                          "moderate", "dog"),
    ("犬瘟肠型",        "gi", "犬瘟肠型,Distemper",                  "severe",   "dog"),
    ("螺杆菌感染",      "gi", "螺杆菌,幽门螺杆菌,Helicobacter",      "moderate", "both"),

    # ── 呼吸系统 ──────────────────────────────────
    ("猫鼻支",          "respiratory", "鼻支,FHV-1,猫疱疹病毒,流鼻涕,打喷嚏,鼻塞", "moderate", "cat"),
    ("猫杯状病毒",      "respiratory", "杯状,FCV,Calicivirus",      "moderate", "cat"),
    ("猫支原体感染",    "respiratory", "支原体,Mycoplasma",         "moderate", "cat"),
    ("猫衣原体感染",    "respiratory", "衣原体,Chlamydia",          "moderate", "cat"),
    ("上呼吸道感染",    "respiratory", "URI,上呼吸道感染",          "mild",     "both"),
    ("犬窝咳",          "respiratory", "犬窝咳,Kennel Cough,CIRDC", "moderate", "dog"),
    ("犬副流感",        "respiratory", "副流感,CPIV",               "moderate", "dog"),
    ("犬流感",          "respiratory", "犬流感,CIV",                "moderate", "dog"),
    ("气管支气管炎",    "respiratory", "气管支气管炎,Tracheobronchitis", "moderate", "both"),
    ("细菌性肺炎",      "respiratory", "细菌性肺炎,Bacterial Pneumonia", "severe", "both"),
    ("病毒性肺炎",      "respiratory", "病毒性肺炎",                "severe",   "both"),
    ("真菌性肺炎",      "respiratory", "真菌性肺炎,Aspergillosis",  "severe",   "both"),
    ("吸入性肺炎",      "respiratory", "吸入性肺炎,Aspiration Pneumonia", "severe", "both"),
    ("猫哮喘",          "respiratory", "猫哮喘,Feline Asthma",      "chronic",  "cat"),
    ("慢性支气管炎",    "respiratory", "慢性支气管炎",              "chronic",  "both"),
    ("肺水肿",          "respiratory", "肺水肿,Pulmonary Edema",   "severe",   "both"),
    ("胸腔积液",        "respiratory", "胸腔积液,Pleural Effusion","severe",    "both"),
    ("乳糜胸",          "respiratory", "乳糜胸,Chylothorax",        "chronic",  "both"),
    ("气胸",            "respiratory", "气胸,Pneumothorax",         "severe",   "both"),
    ("气管塌陷",        "respiratory", "气管塌陷,Tracheal Collapse","chronic",  "dog"),
    ("喉麻痹",          "respiratory", "喉麻痹,Laryngeal Paralysis","chronic",  "dog"),
    ("短头综合征",      "respiratory", "短头综合征,BOAS,短鼻综合征","chronic",  "dog"),
    ("鼻炎",            "respiratory", "鼻炎,Rhinitis",             "mild",     "both"),
    ("鼻窦炎",          "respiratory", "鼻窦炎,Sinusitis",          "moderate", "both"),
    ("鼻咽息肉",        "respiratory", "鼻咽息肉,Nasopharyngeal Polyp", "moderate", "cat"),

    # ── 皮肤系统 ──────────────────────────────────
    ("特应性皮炎",      "skin", "特应性皮炎,异位性皮炎,AD,Atopic Dermatitis", "chronic", "both"),
    ("食物过敏性皮炎",  "skin", "食物过敏性皮炎,CAFR",              "chronic",  "both"),
    ("跳蚤过敏性皮炎",  "skin", "跳蚤过敏性皮炎,FAD",               "moderate", "both"),
    ("接触性皮炎",      "skin", "接触性皮炎",                       "mild",     "both"),
    ("脂溢性皮炎",      "skin", "脂溢性皮炎,Seborrhea",             "chronic",  "both"),
    ("浅表脓皮病",      "skin", "浅表脓皮病,Superficial Pyoderma", "moderate", "both"),
    ("深部脓皮病",      "skin", "深部脓皮病,Deep Pyoderma",        "severe",   "both"),
    ("毛囊炎",          "skin", "毛囊炎,Folliculitis",              "mild",     "both"),
    ("趾间脓皮病",      "skin", "趾间脓皮病,Interdigital Pyoderma","moderate", "dog"),
    ("皮肤癣菌病",      "skin", "癣,真菌性皮炎,皮肤癣菌,Dermatophytosis", "moderate", "both"),
    ("马拉色菌皮炎",    "skin", "马拉色菌,Malassezia",              "moderate", "both"),
    ("蠕形螨病",        "skin", "蠕形螨,Demodex,毛囊虫",            "moderate", "dog"),
    ("疥螨病",          "skin", "疥螨,Sarcoptes,Scabies",          "moderate", "both"),
    ("耳螨",            "skin", "耳螨,Otodectes",                  "mild",     "both"),
    ("跳蚤感染",        "skin", "跳蚤,Flea",                        "mild",     "both"),
    ("蜱虫感染",        "skin", "蜱虫,Tick",                        "moderate", "both"),
    ("嗜酸性肉芽肿综合征", "skin", "EGC,嗜酸性肉芽肿,猫粟粒性皮炎", "chronic",  "cat"),
    ("天疱疮",          "skin", "天疱疮,Pemphigus",                "chronic",  "both"),
    ("红斑狼疮",        "skin", "红斑狼疮,Lupus,DLE,SLE",          "chronic",  "both"),
    ("内分泌性脱毛",    "skin", "内分泌脱毛,Endocrine Alopecia",   "chronic",  "both"),
    ("肛周瘘",          "skin", "肛周瘘,Perianal Fistula",         "chronic",  "dog"),
    ("外耳炎",          "skin", "外耳炎,Otitis Externa,耳朵痒,耳朵臭,耳屎多", "moderate", "both"),
    ("中耳炎",          "skin", "中耳炎,Otitis Media",             "moderate", "both"),

    # ── 口腔牙科 ──────────────────────────────────
    ("牙结石",          "dental", "牙结石,牙石",                   "mild",     "both"),
    ("牙龈炎",          "dental", "牙龈炎,Gingivitis",             "mild",     "both"),
    ("牙周病",          "dental", "牙周病,牙周炎,Periodontitis",   "chronic",  "both"),
    ("慢性口炎",        "dental", "慢性口炎,FCGS,FORL口炎",        "chronic",  "cat"),
    ("浆细胞口炎",      "dental", "浆细胞口炎",                    "chronic",  "cat"),
    ("猫牙吸收",        "dental", "FORL,牙吸收,牙颈部吸收",        "chronic",  "cat"),
    ("牙折",            "dental", "牙折,牙齿折断",                 "moderate", "both"),
    ("牙髓暴露",        "dental", "牙髓暴露",                      "severe",   "both"),
    ("乳牙滞留",        "dental", "乳牙滞留,Retained Deciduous",   "mild",     "both"),
    ("口腔肿瘤",        "dental", "口腔肿瘤,口腔鳞癌,SCC",         "severe",   "both"),
    ("颌骨骨折",        "dental", "颌骨骨折,下颌骨折",             "severe",   "both"),

    # ── 眼科 ─────────────────────────────────────
    ("角膜炎",          "ophthalmic", "角膜炎,Keratitis",          "moderate", "both"),
    ("角膜溃疡",        "ophthalmic", "角膜溃疡,Corneal Ulcer",    "severe",   "both"),
    ("角膜穿孔",        "ophthalmic", "角膜穿孔",                  "severe",   "both"),
    ("嗜酸性角膜炎",    "ophthalmic", "嗜酸性角膜炎,EK",           "chronic",  "cat"),
    ("结膜炎",          "ophthalmic", "结膜炎,Conjunctivitis,眼屎多,流眼泪,眼睛红", "mild", "both"),
    ("干眼症",          "ophthalmic", "干眼症,KCS,泪液减少",       "chronic",  "dog"),
    ("葡萄膜炎",        "ophthalmic", "葡萄膜炎,Uveitis",          "moderate", "both"),
    ("白内障",          "ophthalmic", "白内障,Cataract",           "chronic",  "both"),
    ("青光眼",          "ophthalmic", "青光眼,Glaucoma",           "severe",   "both"),
    ("视网膜变性",      "ophthalmic", "PRA,视网膜萎缩",             "chronic",  "both"),
    ("视网膜脱离",      "ophthalmic", "视网膜脱离",                "severe",   "both"),
    ("睑内翻",          "ophthalmic", "睑内翻,Entropion",          "moderate", "both"),
    ("睑外翻",          "ophthalmic", "睑外翻,Ectropion",          "moderate", "dog"),
    ("樱桃眼",          "ophthalmic", "樱桃眼,Cherry Eye",         "moderate", "dog"),
    ("第三眼睑突出",    "ophthalmic", "第三眼睑突出,Haw Syndrome", "mild",     "cat"),

    # ── 泌尿系统 ─────────────────────────────────
    ("膀胱炎",          "urinary", "膀胱炎,Cystitis,尿频,血尿,小便带血,排尿困难", "moderate", "both"),
    ("猫特发性膀胱炎",  "urinary", "FIC,特发性膀胱炎",              "chronic",  "cat"),
    ("猫下泌尿道综合征","urinary", "FLUTD,FUS",                    "moderate", "cat"),
    ("尿道堵塞",        "urinary", "尿道堵塞,尿闭,Urethral Obstruction", "severe", "cat"),
    ("膀胱结石",        "urinary", "膀胱结石,Cystic Calculi",      "moderate", "both"),
    ("草酸钙结石",      "urinary", "草酸钙结石,Calcium Oxalate",   "moderate", "both"),
    ("鸟粪石",          "urinary", "鸟粪石,磷酸铵镁,Struvite",     "moderate", "both"),
    ("尿酸盐结石",      "urinary", "尿酸盐结石,Urate",             "moderate", "dog"),
    ("胱氨酸结石",      "urinary", "胱氨酸结石,Cystine",           "moderate", "dog"),
    ("尿道结石",        "urinary", "尿道结石",                     "severe",   "both"),
    ("尿失禁",          "urinary", "尿失禁,Incontinence",          "chronic",  "both"),
    ("尿路感染",        "urinary", "UTI,尿路感染",                 "moderate", "both"),

    # ── 肾脏 ─────────────────────────────────────
    ("慢性肾病",        "renal", "CKD,慢性肾病,慢性肾衰",          "chronic",  "both"),
    ("急性肾损伤",      "renal", "AKI,急性肾损伤,急性肾衰",        "severe",   "both"),
    ("肾盂肾炎",        "renal", "肾盂肾炎,Pyelonephritis",        "moderate", "both"),
    ("肾积水",          "renal", "肾积水,Hydronephrosis",          "moderate", "both"),
    ("肾结石",          "renal", "肾结石,Nephrolithiasis",         "chronic",  "both"),
    ("多囊肾",          "renal", "多囊肾,PKD",                     "chronic",  "cat"),
    ("蛋白尿",          "renal", "蛋白尿,Proteinuria",             "chronic",  "both"),
    ("肾小球肾炎",      "renal", "肾小球肾炎,GN",                  "chronic",  "both"),

    # ── 心血管 ───────────────────────────────────
    ("二尖瓣关闭不全",  "cardio", "MMVD,二尖瓣闭锁不全,粘液样瓣膜病", "chronic", "dog"),
    ("肥厚性心肌病",    "cardio", "HCM,肥厚性心肌病",              "chronic",  "cat"),
    ("扩张性心肌病",    "cardio", "DCM,扩张性心肌病",              "chronic",  "dog"),
    ("限制性心肌病",    "cardio", "RCM,限制性心肌病",              "chronic",  "cat"),
    ("充血性心力衰竭",  "cardio", "CHF,充血性心衰,心衰",          "severe",   "both"),
    ("心包积液",        "cardio", "心包积液,Pericardial Effusion","severe",    "both"),
    ("心律失常",        "cardio", "心律失常,Arrhythmia",          "moderate",  "both"),
    ("房颤",            "cardio", "房颤,AFib,Atrial Fibrillation","moderate",  "dog"),
    ("动脉导管未闭",    "cardio", "PDA,动脉导管未闭",             "severe",    "both"),
    ("高血压",          "cardio", "高血压,Hypertension",          "chronic",   "both"),
    ("血栓栓塞",        "cardio", "ATE,动脉血栓栓塞,鞍状血栓",     "severe",   "cat"),

    # ── 神经系统 ─────────────────────────────────
    ("癫痫",            "neuro", "癫痫,Epilepsy,Seizure",         "chronic",  "both"),
    ("前庭综合征",      "neuro", "前庭综合征,Vestibular",         "moderate", "both"),
    ("椎间盘突出",      "neuro", "IVDD,椎间盘突出,椎间盘疾病",    "severe",   "dog"),
    ("脊髓损伤",        "neuro", "脊髓损伤",                      "severe",   "both"),
    ("脑炎",            "neuro", "脑炎,Encephalitis,GME,MUE",     "severe",   "both"),
    ("脑膜炎",          "neuro", "脑膜炎,Meningitis",             "severe",   "both"),
    ("脑积水",          "neuro", "脑积水,Hydrocephalus",          "chronic",  "both"),
    ("认知功能障碍",    "neuro", "CDS,认知障碍,老年痴呆",          "chronic",  "both"),
    ("共济失调",        "neuro", "共济失调,Ataxia",               "moderate", "both"),

    # ── 内分泌 ───────────────────────────────────
    ("糖尿病",          "endocrine", "DM,糖尿病,Diabetes",         "chronic",  "both"),
    ("糖尿病酮症酸中毒","endocrine", "DKA,酮症酸中毒",             "severe",   "both"),
    ("库欣综合征",      "endocrine", "库欣,HAC,肾上腺皮质亢进",    "chronic",  "dog"),
    ("艾迪生病",        "endocrine", "艾迪生,HOAC,肾上腺皮质机能减退","chronic","dog"),
    ("甲状腺机能亢进",  "endocrine", "甲亢,Hyperthyroidism",       "chronic",  "cat"),
    ("甲状腺机能减退",  "endocrine", "甲减,Hypothyroidism",        "chronic",  "dog"),
    ("胰岛素瘤",        "endocrine", "胰岛素瘤,Insulinoma",        "severe",   "dog"),
    ("尿崩症",          "endocrine", "尿崩症,Diabetes Insipidus", "chronic",  "both"),

    # ── 血液/免疫 ────────────────────────────────
    ("免疫介导性溶血性贫血", "hemato", "IMHA,自身免疫性溶血性贫血", "severe", "both"),
    ("免疫介导性血小板减少", "hemato", "IMTP,ITP,免疫性血小板减少", "severe", "both"),
    ("再生障碍性贫血",   "hemato", "再障,Aplastic Anemia",         "severe",  "both"),
    ("猫白血病病毒感染", "hemato", "FeLV,猫白血病",                 "chronic", "cat"),
    ("猫免疫缺陷病毒",   "hemato", "FIV,猫艾滋",                    "chronic", "cat"),
    ("巴贝斯虫感染",     "hemato", "巴贝斯虫,Babesia",              "severe",  "dog"),
    ("埃立克体感染",     "hemato", "埃立克体,Ehrlichia",            "moderate","dog"),
    ("立克次体感染",     "hemato", "立克次体,Rickettsia",           "moderate","both"),
    ("利什曼原虫病",     "hemato", "利什曼,Leishmania",             "chronic", "dog"),

    # ── 肿瘤 ─────────────────────────────────────
    ("淋巴瘤",          "oncology", "淋巴瘤,Lymphoma,LSA",         "severe",  "both"),
    ("肥大细胞瘤",      "oncology", "MCT,肥大细胞瘤",              "moderate","both"),
    ("血管肉瘤",        "oncology", "血管肉瘤,HSA,Hemangiosarcoma","severe",  "dog"),
    ("乳腺肿瘤",        "oncology", "乳腺肿瘤,Mammary Tumor",      "severe",  "both"),
    ("骨肉瘤",          "oncology", "骨肉瘤,Osteosarcoma,OSA",     "severe",  "dog"),
    ("黑色素瘤",        "oncology", "黑色素瘤,Melanoma",           "severe",  "both"),
    ("鳞状细胞癌",      "oncology", "SCC,鳞状细胞癌,鳞癌",         "severe",  "both"),
    ("纤维肉瘤",        "oncology", "纤维肉瘤,FSA",                "moderate","both"),
    ("脂肪瘤",          "oncology", "脂肪瘤,Lipoma",               "mild",    "dog"),

    # ── 骨科 ─────────────────────────────────────
    ("骨折",            "ortho", "骨折,Fracture",                  "severe",  "both"),
    ("髋关节发育不良",  "ortho", "髋发育不良,HD",                  "chronic", "dog"),
    ("肘关节发育不良",  "ortho", "肘发育不良,ED",                  "chronic", "dog"),
    ("膝十字韧带断裂",  "ortho", "CCL,ACL,前十字韧带断裂",         "severe",  "dog"),
    ("髌骨脱位",        "ortho", "髌骨脱位,Patellar Luxation",     "moderate","both"),
    ("骨关节炎",        "ortho", "骨关节炎,OA,Osteoarthritis",    "chronic", "both"),
    ("股骨头坏死",      "ortho", "股骨头坏死,LCP",                 "chronic", "dog"),

    # ── 生殖/产科 ────────────────────────────────
    ("子宫蓄脓",        "reproduction", "子宫蓄脓,Pyometra",       "severe",  "both"),
    ("假孕",            "reproduction", "假孕,Pseudopregnancy",    "mild",    "dog"),
    ("难产",            "reproduction", "难产,Dystocia",           "severe",  "both"),
    ("产后子宫复旧不全", "reproduction", "子宫复旧不全",            "moderate","both"),
    ("乳腺炎",          "reproduction", "乳腺炎,Mastitis",         "moderate","both"),
    ("前列腺增生",      "reproduction", "前列腺增生,BPH",          "chronic", "dog"),
    ("隐睾",            "reproduction", "隐睾,Cryptorchidism",     "chronic", "both"),

    # ── 传染病（综合） ───────────────────────────
    ("狂犬病",          "infectious", "狂犬病,Rabies",            "severe",  "both"),
    ("钩端螺旋体病",    "infectious", "钩端螺旋体,Leptospirosis", "severe",  "dog"),
    ("布鲁氏菌病",      "infectious", "布鲁氏菌,Brucella",        "chronic", "dog"),
    ("莱姆病",          "infectious", "莱姆病,Lyme,伯氏疏螺旋体", "chronic", "dog"),

    # ── 手术专项（用于回访匹配，不一定是「疾病」） ──
    ("绝育术",          "surgical", "绝育,卵巢摘除,子宫摘除,OHE,OVH,去势,阉割,睾丸摘除", "moderate", "both"),
    ("剖腹产",          "surgical", "剖腹产,Cesarean",            "severe",  "both"),
    ("软组织肿物切除",  "surgical", "肿物切除,肿瘤切除",          "moderate", "both"),
    ("骨折内固定术",    "surgical", "骨折内固定,接骨术",          "severe",  "both"),
    ("膈疝修复",        "surgical", "膈疝,Diaphragmatic Hernia", "severe",  "both"),
    ("脾摘除术",        "surgical", "脾摘除,Splenectomy",         "severe",  "both"),
    ("膀胱切开取石",    "surgical", "膀胱切开,Cystotomy",         "moderate","both"),
    ("会阴尿道造口术",  "surgical", "PU,会阴尿道造口",            "severe",  "cat"),
    ("肠吻合术",        "surgical", "肠吻合,Enteroanastomosis",   "severe",  "both"),
    ("洗牙",            "surgical", "洗牙,Dental Cleaning,Scaling","mild",   "both"),
    ("拔牙",            "surgical", "拔牙,Extraction",            "moderate","both"),
]


# ═══════════════════════════════════════════════════════════════
# 问题预设：复用减少重复
# ═══════════════════════════════════════════════════════════════
Q = {
    "spirit":    {"key": "spirit",    "type": "scale1to5", "label": "精神状态",  "help": "1=萎靡 · 5=活泼"},
    "appetite":  {"key": "appetite",  "type": "scale1to5", "label": "食欲",      "help": "1=不吃 · 5=正常"},
    "water":     {"key": "water",     "type": "scale1to5", "label": "饮水",      "help": "1=不喝 · 5=正常"},
    "energy":    {"key": "energy",    "type": "scale1to5", "label": "活动量",    "help": "1=完全卧床 · 5=活泼蹦跳"},
    "stool":     {"key": "stool",     "type": "select",    "label": "排便情况",
                  "options": ["正常", "软便", "稀便/拉稀", "便血", "便秘", "未排便"]},
    "stool_freq":{"key": "stool_freq","type": "select",    "label": "排便频率",
                  "options": ["正常 1-2次/天", "增多", "减少", "完全没拉"]},
    "vomit":     {"key": "vomit",     "type": "select",    "label": "呕吐",
                  "options": ["无", "偶尔 1次", "2-3次", "频繁(>3次)", "喷射性呕吐"]},
    "urine":     {"key": "urine",     "type": "select",    "label": "小便",
                  "options": ["正常", "量多", "量少", "血尿", "频繁但量少", "排尿困难/嚎叫"]},
    "wound":     {"key": "wound",     "type": "select",    "label": "伤口情况",
                  "options": ["干燥愈合", "少量渗液", "红肿热痛", "裂开", "化脓"]},
    "cough":     {"key": "cough",     "type": "select",    "label": "咳嗽",
                  "options": ["无", "偶尔", "频繁", "剧烈/伴呕逆", "夜间加重"]},
    "sneeze":    {"key": "sneeze",    "type": "select",    "label": "打喷嚏/鼻涕",
                  "options": ["无", "少量清涕", "脓性鼻涕", "带血鼻涕"]},
    "breath":    {"key": "breath",    "type": "select",    "label": "呼吸",
                  "options": ["正常", "稍急促", "明显急促", "张口呼吸", "舌头/牙龈发紫"]},
    "itch":      {"key": "itch",      "type": "scale1to5", "label": "瘙痒程度",  "help": "1=无 · 5=严重抓挠"},
    "skin":      {"key": "skin",      "type": "select",    "label": "皮损变化",
                  "options": ["明显改善", "略改善", "无变化", "加重", "出现新皮损"]},
    "eye":       {"key": "eye",       "type": "select",    "label": "眼部分泌物/红肿",
                  "options": ["明显改善", "略改善", "无变化", "加重"]},
    "weight":    {"key": "weight",    "type": "number",    "label": "最新体重(kg)", "step": 0.01},
    "med_taken": {"key": "med_taken", "type": "select",    "label": "按时给药",
                  "options": ["完全按时", "偶尔漏一两次", "经常漏", "完全没喂", "宠物拒食"]},
    "med_side":  {"key": "med_side",  "type": "text",      "label": "用药不良反应（如有）"},
    "photo":     {"key": "photos",    "type": "upload",    "label": "现状照片", "max": 3,
                  "help": "伤口/皮损/便便/呕吐物等都可"},
    "note":      {"key": "note",      "type": "text",      "label": "其他想告诉医生的"},
    "needs_visit":{"key":"needs_visit","type": "select",   "label": "需要复诊吗？",
                  "options": ["不需要，已好转", "希望线上咨询", "需要复诊", "紧急情况需立即就诊"]},
}


# ═══════════════════════════════════════════════════════════════
# 回访模板（含多轮 + 结构化问题）
# 优先级：
#   100 = 急性大手术（优先匹配，自动衍生多轮）
#    80 = 一般手术 / 急性病
#    50 = 慢病专科
#    30 = 一般门诊
# ═══════════════════════════════════════════════════════════════
_KW = lambda *xs: ",".join(xs)

TEMPLATES = [
    # ── 绝育术后 ──────────────────────────────────
    {
        "name": "绝育术后",
        "system": "surgical",
        "priority": 100,
        "keywords": _KW("绝育", "卵巢摘除", "子宫摘除", "OHE", "OVH", "去势", "阉割", "睾丸摘除", "卵巢子宫摘除"),
        "rounds": [
            {"day_offset": 3,  "round_name": "术后 3 天 · 伤口检查",
             "questions": [Q["spirit"], Q["appetite"], Q["wound"], Q["vomit"], Q["photo"], Q["note"]]},
            {"day_offset": 7,  "round_name": "术后 7 天 · 拆线前",
             "questions": [Q["wound"], Q["energy"], Q["appetite"], Q["photo"], Q["needs_visit"]]},
            {"day_offset": 14, "round_name": "术后 14 天 · 恢复确认",
             "questions": [Q["energy"], Q["weight"], Q["note"]]},
        ],
    },
    # ── 大型软组织手术 ─────────────────────────────
    {
        "name": "大型软组织手术",
        "system": "surgical",
        "priority": 100,
        "keywords": _KW("肿物切除", "肿瘤切除", "脾摘除", "肠吻合", "膈疝", "异物取出", "膀胱切开", "PU",
                        "会阴尿道造口", "剖腹产", "胆囊切除"),
        "rounds": [
            {"day_offset": 1,  "round_name": "术后 24h · 麻醉苏醒",
             "questions": [Q["spirit"], Q["appetite"], Q["vomit"], Q["urine"], Q["needs_visit"], Q["note"]]},
            {"day_offset": 3,  "round_name": "术后 3 天 · 伤口检查",
             "questions": [Q["wound"], Q["appetite"], Q["spirit"], Q["photo"], Q["note"]]},
            {"day_offset": 7,  "round_name": "术后 7 天 · 拆线前",
             "questions": [Q["wound"], Q["energy"], Q["med_taken"], Q["photo"]]},
            {"day_offset": 14, "round_name": "术后 14 天 · 恢复确认",
             "questions": [Q["energy"], Q["weight"], Q["needs_visit"]]},
        ],
    },
    # ── 骨科手术 ──────────────────────────────────
    {
        "name": "骨科术后",
        "system": "ortho",
        "priority": 90,
        "keywords": _KW("骨折", "骨折内固定", "接骨术", "髌骨脱位", "ACL", "CCL", "十字韧带", "TPLO", "TTA",
                        "截肢", "髋关节", "肘关节"),
        "rounds": [
            {"day_offset": 3,  "round_name": "术后 3 天",
             "questions": [Q["wound"], Q["spirit"], Q["appetite"], Q["energy"], Q["photo"]]},
            {"day_offset": 14, "round_name": "术后 14 天 · 复查 X 光",
             "questions": [Q["energy"], Q["wound"], Q["needs_visit"]]},
            {"day_offset": 30, "round_name": "术后 30 天 · 复查 X 光",
             "questions": [Q["energy"], Q["weight"], Q["note"], Q["needs_visit"]]},
            {"day_offset": 60, "round_name": "术后 60 天 · 复查 X 光",
             "questions": [Q["energy"], Q["weight"], Q["needs_visit"]]},
        ],
    },
    # ── 牙科手术 ──────────────────────────────────
    {
        "name": "牙科术后（洗牙/拔牙）",
        "system": "dental",
        "priority": 85,
        "keywords": _KW("洗牙", "拔牙", "牙周手术", "Extraction", "Scaling", "牙周翻瓣"),
        "rounds": [
            {"day_offset": 3,  "round_name": "术后 3 天 · 口腔/食欲",
             "questions": [Q["appetite"], Q["spirit"], Q["vomit"], Q["med_taken"], Q["photo"], Q["note"]]},
            {"day_offset": 14, "round_name": "术后 14 天 · 恢复",
             "questions": [Q["appetite"], Q["energy"], Q["needs_visit"]]},
        ],
    },
    # ── 消化系统（急性） ──────────────────────────
    {
        "name": "消化系统疾病",
        "system": "gi",
        "priority": 60,
        "keywords": _KW(
            "肠炎", "胃肠炎", "AGE", "胃炎", "急性胃炎", "慢性胃炎",
            "IBD", "炎症性肠病", "胰腺炎", "巨结肠", "便秘",
            "腹泻", "呕吐", "HGE", "出血性肠炎", "嗜酸性肠炎", "PLE",
            "肝炎", "胆管炎", "胆囊", "脂肪肝", "三联炎",
            "食物过敏", "食物不耐受",
            "蛔虫", "绦虫", "钩虫", "球虫", "贾第虫", "弓形虫",
            "猫冠状", "FCoV", "FECV", "FIP", "传染性腹膜炎", "细小", "Parvo",
            "幽门螺杆菌", "螺杆菌",
        ),
        "rounds": [
            {"day_offset": 2,  "round_name": "用药 2 天 · 症状变化",
             "questions": [Q["spirit"], Q["appetite"], Q["vomit"], Q["stool"], Q["med_taken"], Q["med_side"], Q["photo"]]},
            {"day_offset": 7,  "round_name": "1 周复查",
             "questions": [Q["appetite"], Q["weight"], Q["stool"], Q["vomit"], Q["needs_visit"]]},
            {"day_offset": 14, "round_name": "2 周转归确认",
             "questions": [Q["appetite"], Q["weight"], Q["needs_visit"], Q["note"]]},
        ],
    },
    # ── 呼吸系统 ──────────────────────────────────
    {
        "name": "呼吸系统疾病",
        "system": "respiratory",
        "priority": 60,
        "keywords": _KW(
            "鼻支", "FHV", "杯状", "FCV", "支原体", "衣原体", "副流感", "犬流感", "URI",
            "上呼吸道感染", "犬窝咳", "CIRDC", "气管支气管炎",
            "肺炎", "细菌性肺炎", "病毒性肺炎", "真菌性肺炎", "吸入性肺炎",
            "哮喘", "猫哮喘", "慢性支气管炎", "肺水肿", "胸腔积液", "气胸", "乳糜胸",
            "鼻炎", "鼻窦炎", "气管塌陷", "喉麻痹", "短头综合征", "BOAS",
        ),
        "rounds": [
            {"day_offset": 3, "round_name": "用药 3 天 · 症状变化",
             "questions": [Q["spirit"], Q["appetite"], Q["sneeze"], Q["cough"], Q["breath"], Q["med_taken"], Q["photo"]]},
            {"day_offset": 10, "round_name": "10 天复查",
             "questions": [Q["cough"], Q["sneeze"], Q["breath"], Q["appetite"], Q["needs_visit"]]},
            {"day_offset": 21, "round_name": "3 周转归确认",
             "questions": [Q["energy"], Q["cough"], Q["needs_visit"]]},
        ],
    },
    # ── 皮肤系统 ──────────────────────────────────
    {
        "name": "皮肤系统疾病",
        "system": "skin",
        "priority": 55,
        "keywords": _KW(
            "皮炎", "特应性皮炎", "异位性皮炎", "AD", "食物过敏性皮炎", "CAFR",
            "跳蚤过敏", "FAD", "接触性皮炎", "脂溢性皮炎",
            "癣", "真菌性皮炎", "皮肤癣菌", "Dermatophytosis", "马拉色菌", "Malassezia",
            "螨虫", "蠕形螨", "Demodex", "疥螨", "Sarcoptes", "Scabies",
            "耳螨", "外耳炎", "中耳炎", "Otitis",
            "脓皮病", "Pyoderma", "毛囊炎", "趾间脓皮病",
            "嗜酸性肉芽肿", "EGC", "天疱疮", "红斑狼疮",
            "脱毛", "内分泌脱毛", "肛周瘘",
            "跳蚤", "蜱虫",
        ),
        "rounds": [
            {"day_offset": 7,  "round_name": "用药 1 周 · 皮损变化",
             "questions": [Q["itch"], Q["skin"], Q["med_taken"], Q["med_side"], Q["photo"]]},
            {"day_offset": 21, "round_name": "3 周复查",
             "questions": [Q["itch"], Q["skin"], Q["photo"], Q["needs_visit"]]},
            {"day_offset": 60, "round_name": "2 个月转归",
             "questions": [Q["itch"], Q["skin"], Q["photo"], Q["needs_visit"]]},
        ],
    },
    # ── 眼科 ─────────────────────────────────────
    {
        "name": "眼科疾病",
        "system": "ophthalmic",
        "priority": 55,
        "keywords": _KW(
            "角膜炎", "角膜溃疡", "角膜穿孔", "嗜酸性角膜炎", "EK",
            "结膜炎", "干眼症", "KCS", "葡萄膜炎", "白内障", "青光眼",
            "视网膜变性", "PRA", "视网膜脱离",
            "睑内翻", "Entropion", "睑外翻", "Ectropion", "樱桃眼",
        ),
        "rounds": [
            {"day_offset": 3,  "round_name": "用药 3 天 · 眼部观察",
             "questions": [Q["eye"], Q["med_taken"], Q["med_side"], Q["photo"]]},
            {"day_offset": 14, "round_name": "2 周复查",
             "questions": [Q["eye"], Q["photo"], Q["needs_visit"]]},
        ],
    },
    # ── 泌尿系统 ─────────────────────────────────
    {
        "name": "泌尿系统疾病",
        "system": "urinary",
        "priority": 60,
        "keywords": _KW(
            "膀胱炎", "Cystitis", "FIC", "特发性膀胱炎", "FLUTD", "FUS",
            "尿道堵塞", "尿闭", "Obstruction",
            "膀胱结石", "草酸钙结石", "鸟粪石", "尿酸盐结石", "胱氨酸结石", "尿道结石",
            "尿失禁", "UTI", "尿路感染",
        ),
        "rounds": [
            {"day_offset": 3, "round_name": "用药 3 天 · 排尿观察",
             "questions": [Q["urine"], Q["spirit"], Q["appetite"], Q["water"], Q["med_taken"], Q["photo"]]},
            {"day_offset": 14, "round_name": "2 周复查",
             "questions": [Q["urine"], Q["water"], Q["weight"], Q["needs_visit"]]},
        ],
    },
    # ── 慢病：肾病 ────────────────────────────────
    {
        "name": "肾病慢病管理",
        "system": "renal",
        "priority": 40,
        "keywords": _KW(
            "CKD", "慢性肾病", "慢性肾衰", "AKI", "急性肾损伤", "急性肾衰",
            "肾盂肾炎", "肾积水", "肾结石", "多囊肾", "PKD",
            "蛋白尿", "肾小球肾炎", "氮质血症",
        ),
        "rounds": [
            {"day_offset": 14, "round_name": "2 周 · 饮水/食欲",
             "questions": [Q["water"], Q["appetite"], Q["weight"], Q["vomit"], Q["urine"], Q["energy"], Q["note"]]},
            {"day_offset": 30, "round_name": "1 月 · 是否需复查肾值",
             "questions": [Q["water"], Q["appetite"], Q["weight"], Q["needs_visit"]]},
            {"day_offset": 90, "round_name": "3 月 · 长期管理",
             "questions": [Q["water"], Q["appetite"], Q["weight"], Q["needs_visit"]]},
        ],
    },
    # ── 慢病：糖尿病 ───────────────────────────────
    {
        "name": "糖尿病慢病管理",
        "system": "endocrine",
        "priority": 40,
        "keywords": _KW("糖尿病", "DM", "Diabetes", "胰岛素", "DKA", "酮症酸中毒", "高血糖", "低血糖"),
        "rounds": [
            {"day_offset": 7,  "round_name": "1 周 · 血糖控制",
             "questions": [Q["spirit"], Q["water"], Q["appetite"], Q["urine"], Q["med_taken"], Q["med_side"]]},
            {"day_offset": 30, "round_name": "1 月 · 复查血糖曲线",
             "questions": [Q["water"], Q["weight"], Q["energy"], Q["needs_visit"]]},
        ],
    },
    # ── 慢病：甲亢/库欣 ───────────────────────────
    {
        "name": "内分泌慢病（甲亢/库欣等）",
        "system": "endocrine",
        "priority": 40,
        "keywords": _KW(
            "甲亢", "Hyperthyroidism", "甲减", "Hypothyroidism",
            "库欣", "HAC", "肾上腺皮质亢进", "艾迪生", "HOAC", "肾上腺皮质机能减退",
            "胰岛素瘤", "Insulinoma", "尿崩症",
        ),
        "rounds": [
            {"day_offset": 14, "round_name": "2 周 · 用药反应",
             "questions": [Q["energy"], Q["appetite"], Q["weight"], Q["water"], Q["med_taken"], Q["med_side"]]},
            {"day_offset": 60, "round_name": "2 月 · 复查激素水平",
             "questions": [Q["energy"], Q["weight"], Q["needs_visit"]]},
        ],
    },
    # ── 心血管慢病 ────────────────────────────────
    {
        "name": "心血管慢病管理",
        "system": "cardio",
        "priority": 45,
        "keywords": _KW(
            "MMVD", "二尖瓣", "粘液样瓣膜病", "HCM", "肥厚性心肌病", "DCM", "扩张性心肌病",
            "RCM", "心衰", "CHF", "充血性心力衰竭", "心包积液", "心律失常", "房颤",
            "PDA", "高血压", "ATE", "鞍状血栓",
        ),
        "rounds": [
            {"day_offset": 7,  "round_name": "1 周 · 呼吸/活动",
             "questions": [Q["breath"], Q["cough"], Q["energy"], Q["appetite"], Q["med_taken"], Q["med_side"]]},
            {"day_offset": 30, "round_name": "1 月 · 复查心脏",
             "questions": [Q["breath"], Q["cough"], Q["energy"], Q["needs_visit"]]},
        ],
    },
    # ── 神经科 ───────────────────────────────────
    {
        "name": "神经系统疾病",
        "system": "neuro",
        "priority": 50,
        "keywords": _KW(
            "癫痫", "抽搐", "前庭综合征", "IVDD", "椎间盘", "脊髓损伤",
            "脑炎", "脑膜炎", "脑积水", "认知障碍", "CDS", "共济失调",
        ),
        "rounds": [
            {"day_offset": 7,  "round_name": "1 周 · 发作频次/活动",
             "questions": [Q["energy"], Q["spirit"], Q["med_taken"], Q["med_side"], Q["note"]]},
            {"day_offset": 30, "round_name": "1 月 · 控制情况",
             "questions": [Q["energy"], Q["med_taken"], Q["needs_visit"]]},
        ],
    },
    # ── 口腔/口炎 ────────────────────────────────
    {
        "name": "口腔慢病（口炎/牙周病）",
        "system": "dental",
        "priority": 50,
        "keywords": _KW(
            "牙结石", "牙龈炎", "牙周病", "牙周炎",
            "慢性口炎", "FCGS", "浆细胞口炎", "FORL", "猫牙吸收",
        ),
        "rounds": [
            {"day_offset": 7,  "round_name": "1 周 · 进食改善",
             "questions": [Q["appetite"], Q["spirit"], Q["med_taken"], Q["photo"], Q["note"]]},
            {"day_offset": 30, "round_name": "1 月 · 复诊",
             "questions": [Q["appetite"], Q["weight"], Q["needs_visit"]]},
        ],
    },
    # ── 肿瘤 ─────────────────────────────────────
    {
        "name": "肿瘤管理",
        "system": "oncology",
        "priority": 50,
        "keywords": _KW(
            "淋巴瘤", "Lymphoma", "MCT", "肥大细胞瘤", "血管肉瘤", "HSA",
            "乳腺肿瘤", "Mammary", "骨肉瘤", "OSA", "黑色素瘤", "Melanoma",
            "鳞状细胞癌", "SCC", "纤维肉瘤", "FSA",
        ),
        "rounds": [
            {"day_offset": 7,  "round_name": "化疗/治疗 1 周",
             "questions": [Q["spirit"], Q["appetite"], Q["vomit"], Q["med_side"], Q["photo"]]},
            {"day_offset": 21, "round_name": "3 周 · 复查",
             "questions": [Q["energy"], Q["weight"], Q["appetite"], Q["needs_visit"]]},
        ],
    },
    # ── 子宫蓄脓术后 / 难产 ──────────────────────
    {
        "name": "生殖/产科紧急术后",
        "system": "reproduction",
        "priority": 95,
        "keywords": _KW(
            "子宫蓄脓", "Pyometra", "难产", "Dystocia",
            "子宫复旧不全", "乳腺炎", "Mastitis",
        ),
        "rounds": [
            {"day_offset": 1,  "round_name": "术后 24h",
             "questions": [Q["spirit"], Q["appetite"], Q["vomit"], Q["urine"], Q["wound"], Q["needs_visit"]]},
            {"day_offset": 3,  "round_name": "术后 3 天",
             "questions": [Q["wound"], Q["appetite"], Q["spirit"], Q["photo"]]},
            {"day_offset": 14, "round_name": "术后 14 天 · 恢复",
             "questions": [Q["energy"], Q["wound"], Q["needs_visit"]]},
        ],
    },
    # ── 一般门诊（兜底） ────────────────────────
    {
        "name": "一般门诊（默认）",
        "system": "general",
        "priority": 5,
        "keywords": "",   # 空 = 不参与关键词匹配，仅作为 visit_type=outpatient 兜底
        "rounds": [
            {"day_offset": 7, "round_name": "1 周 · 是否好转",
             "questions": [Q["spirit"], Q["appetite"], Q["energy"], Q["needs_visit"], Q["note"]]},
        ],
    },
]
