from typing import List, Dict
from . import schemas


ENTERPRISES_DATA: List[Dict] = [
    {
        "name": "比亚迪汽车工业有限公司",
        "short_name": "比亚迪",
        "credit_code": "91440300192180523K",
        "address": "广东省深圳市坪山新区比亚迪路3009号",
        "contact_person": "王传福",
        "contact_phone": "0755-89888888"
    },
    {
        "name": "特斯拉(上海)有限公司",
        "short_name": "特斯拉",
        "credit_code": "91310000MA1FL5T70E",
        "address": "上海市浦东新区江山路5000号",
        "contact_person": "朱晓彤",
        "contact_phone": "021-60988888"
    },
    {
        "name": "上海汽车集团股份有限公司",
        "short_name": "上汽集团",
        "credit_code": "91310000132260250X",
        "address": "上海市威海路489号",
        "contact_person": "陈虹",
        "contact_phone": "021-22011888"
    },
    {
        "name": "广州小鹏汽车科技有限公司",
        "short_name": "小鹏汽车",
        "credit_code": "91440101MA5D090K0M",
        "address": "广东省广州市天河区岑村松岗大街8号",
        "contact_person": "何小鹏",
        "contact_phone": "020-66806688"
    },
    {
        "name": "北京新能源汽车股份有限公司",
        "short_name": "北汽新能源",
        "credit_code": "91110000101162382J",
        "address": "北京市大兴区采育镇经济开发区育振街1号",
        "contact_person": "刘宇",
        "contact_phone": "010-80278888"
    }
]


def generate_multi_year_models() -> List[Dict]:
    base_models_2025 = [
        {"ent_idx": 0, "model_name": "汉EV", "model_code_prefix": "BYD-HAN-EV",
         "curb_weight": 2250, "base_pc": 16.0, "range": 550, "base_output": 60000},
        {"ent_idx": 0, "model_name": "海豹", "model_code_prefix": "BYD-SEAL",
         "curb_weight": 1980, "base_pc": 13.5, "range": 500, "base_output": 45000},
        {"ent_idx": 0, "model_name": "海豚", "model_code_prefix": "BYD-DOLPHIN",
         "curb_weight": 1405, "base_pc": 11.2, "range": 380, "base_output": 70000},
        {"ent_idx": 0, "model_name": "元PLUS", "model_code_prefix": "BYD-YUAN-PLUS",
         "curb_weight": 1690, "base_pc": 13.0, "range": 460, "base_output": 55000},
        {"ent_idx": 0, "model_name": "唐EV", "model_code_prefix": "BYD-TANG-EV",
         "curb_weight": 2560, "base_pc": 19.5, "range": 560, "base_output": 30000},
        {"ent_idx": 0, "model_name": "护卫舰07", "model_code_prefix": "BYD-FRIGATE-07",
         "curb_weight": 2450, "base_pc": 22.0, "range": 460, "base_output": 20000},

        {"ent_idx": 1, "model_name": "Model 3", "model_code_prefix": "TSLA-M3",
         "curb_weight": 1760, "base_pc": 12.8, "range": 500, "base_output": 75000},
        {"ent_idx": 1, "model_name": "Model Y", "model_code_prefix": "TSLA-MY",
         "curb_weight": 1900, "base_pc": 13.8, "range": 480, "base_output": 120000},
        {"ent_idx": 1, "model_name": "Model S", "model_code_prefix": "TSLA-MS",
         "curb_weight": 2185, "base_pc": 20.0, "range": 600, "base_output": 8000},
        {"ent_idx": 1, "model_name": "Model X", "model_code_prefix": "TSLA-MX",
         "curb_weight": 2455, "base_pc": 23.5, "range": 520, "base_output": 6000},

        {"ent_idx": 2, "model_name": "智己L7", "model_code_prefix": "SAIC-IM-L7",
         "curb_weight": 2320, "base_pc": 24.2, "range": 420, "base_output": 18000},
        {"ent_idx": 2, "model_name": "飞凡F7", "model_code_prefix": "SAIC-F7",
         "curb_weight": 2050, "base_pc": 19.8, "range": 460, "base_output": 28000},
        {"ent_idx": 2, "model_name": "荣威Ei5", "model_code_prefix": "SAIC-ROEWE-EI5",
         "curb_weight": 1610, "base_pc": 14.0, "range": 450, "base_output": 35000},
        {"ent_idx": 2, "model_name": "名爵MG4 EV", "model_code_prefix": "SAIC-MG4-EV",
         "curb_weight": 1685, "base_pc": 14.8, "range": 460, "base_output": 42000},
        {"ent_idx": 2, "model_name": "五菱宏光MINI EV", "model_code_prefix": "SAIC-WULING-MINI",
         "curb_weight": 920, "base_pc": 9.2, "range": 240, "base_output": 150000},

        {"ent_idx": 3, "model_name": "小鹏G6", "model_code_prefix": "XPENG-G6",
         "curb_weight": 1995, "base_pc": 14.0, "range": 620, "base_output": 28000},
        {"ent_idx": 3, "model_name": "小鹏P7", "model_code_prefix": "XPENG-P7",
         "curb_weight": 1910, "base_pc": 14.5, "range": 540, "base_output": 32000},
        {"ent_idx": 3, "model_name": "小鹏G3i", "model_code_prefix": "XPENG-G3I",
         "curb_weight": 1680, "base_pc": 14.2, "range": 460, "base_output": 12000},
        {"ent_idx": 3, "model_name": "小鹏P5", "model_code_prefix": "XPENG-P5",
         "curb_weight": 1775, "base_pc": 14.8, "range": 480, "base_output": 18000},

        {"ent_idx": 4, "model_name": "极狐阿尔法S", "model_code_prefix": "BAIC-ARCFOX-S",
         "curb_weight": 2180, "base_pc": 17.5, "range": 650, "base_output": 6000},
        {"ent_idx": 4, "model_name": "北汽EU5 PLUS", "model_code_prefix": "BAIC-EU5-PLUS",
         "curb_weight": 1650, "base_pc": 18.0, "range": 380, "base_output": 65000},
        {"ent_idx": 4, "model_name": "北汽EU7", "model_code_prefix": "BAIC-EU7",
         "curb_weight": 1755, "base_pc": 18.8, "range": 340, "base_output": 32000},
        {"ent_idx": 4, "model_name": "北汽EX3", "model_code_prefix": "BAIC-EX3",
         "curb_weight": 1580, "base_pc": 16.2, "range": 300, "base_output": 40000},
    ]

    years = [2022, 2023, 2024, 2025]
    pc_improvement = {2022: 1.10, 2023: 1.05, 2024: 1.02, 2025: 1.00}
    output_growth = {2022: 0.70, 2023: 0.85, 2024: 0.92, 2025: 1.00}
    range_growth = {2022: 0.85, 2023: 0.92, 2024: 0.96, 2025: 1.00}

    models = []
    for year in years:
        for base in base_models_2025:
            pc = round(base["base_pc"] * pc_improvement[year], 1)
            output = int(base["base_output"] * output_growth[year])
            range_km = int(base["range"] * range_growth[year])

            model_variants = []
            if year == 2025:
                if base["ent_idx"] == 0:
                    variants = ["创世版", "冠军版", "时尚版", "荣耀版", "四驱版", "活力版", "旗舰版"]
                    variant_idx = base_models_2025.index(base) % len(variants)
                    model_name = f"{base['model_name']} {variants[variant_idx]}"
                elif base["ent_idx"] == 1:
                    variants = ["后轮驱动版", "长续航版", "高性能版", "双电机版", "三电机版"]
                    variant_idx = base_models_2025.index(base) % len(variants)
                    model_name = f"{base['model_name']} {variants[variant_idx]}"
                else:
                    variants = ["Max版", "旗舰版", "长续航版", "出行版", "出海版", "基础版", "先行版", "森林版", "网约车版", "家庭版"]
                    variant_idx = base_models_2025.index(base) % len(variants)
                    model_name = f"{base['model_name']} {variants[variant_idx]}"
            else:
                model_name = f"{base['model_name']}"

            models.append({
                "ent_idx": base["ent_idx"],
                "model_name": model_name,
                "model_code": f"{base['model_code_prefix']}-{year}",
                "curb_weight": base["curb_weight"],
                "power_consumption": pc,
                "range": range_km,
                "annual_output": output,
                "year": year
            })

    return models


VEHICLE_MODELS_DATA = generate_multi_year_models()


def get_enterprise_schemas() -> List[schemas.EnterpriseCreate]:
    return [schemas.EnterpriseCreate(**e) for e in ENTERPRISES_DATA]


def get_vehicle_model_schemas(enterprise_ids: List[int]) -> List[schemas.VehicleModelCreate]:
    models = []
    for m in VEHICLE_MODELS_DATA:
        ent_idx = m.get("ent_idx")
        year = m.get("year")
        model_data = {k: v for k, v in m.items() if k not in ["ent_idx", "year"]}
        models.append(schemas.VehicleModelCreate(
            enterprise_id=enterprise_ids[ent_idx],
            production_year=year,
            **model_data
        ))
    return models


def get_initial_market_orders(enterprise_ids: List[int], year: int = 2025) -> List[Dict]:
    return [
        {
            "enterprise_id": enterprise_ids[0],
            "year": year,
            "order_type": "sell",
            "unit_price": 3000.0,
            "total_amount": 50000.0,
            "remark": "比亚迪2025年度积分钟余出售"
        },
        {
            "enterprise_id": enterprise_ids[1],
            "year": year,
            "order_type": "sell",
            "unit_price": 3050.0,
            "total_amount": 40000.0,
            "remark": "特斯拉2025年度积分钟余出售"
        },
        {
            "enterprise_id": enterprise_ids[2],
            "year": year,
            "order_type": "buy",
            "unit_price": 3100.0,
            "total_amount": 15000.0,
            "remark": "上汽集团2025年度积分缺口采购"
        },
        {
            "enterprise_id": enterprise_ids[3],
            "year": year,
            "order_type": "buy",
            "unit_price": 3080.0,
            "total_amount": 10000.0,
            "remark": "小鹏汽车2025年度积分缺口采购"
        },
        {
            "enterprise_id": enterprise_ids[4],
            "year": year,
            "order_type": "buy",
            "unit_price": 3150.0,
            "total_amount": 60000.0,
            "remark": "北汽新能源2025年度积分缺口采购"
        },
    ]

