import json

import requests

ACTIVE_SIGNS_URL = "https://v18.teachermate.cn/wechat-api/v1/class-attendance/student/active_signs"
STUDENT_INFO_URL = "https://v18.teachermate.cn/wechat-api/v2/students"
STUDENT_ROLE_URL = "https://v18.teachermate.cn/wechat-api/v2/students/role"
SIGN_IN_URL = "https://v18.teachermate.cn/wechat-api/v1/class-attendance/student-sign-in"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 Edg/122.0.0.0"
)


def _build_headers(openid):
    return {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,en-US;q=0.7,en;q=0.3",
        "Openid": openid,
        "Referer": f"https://v18.teachermate.cn/wechat-pro/student/edit?openid={openid}",
    }


def _get_json(url, openid):
    response = requests.get(url, headers=_build_headers(openid), timeout=10)
    return json.loads(response.text)


def _post_json(url, openid, payload):
    headers = dict(_build_headers(openid))
    headers["Content-Type"] = "application/json"
    response = requests.post(url, headers=headers, json=payload, timeout=10)
    return json.loads(response.text)


def getData(openid):
    return _get_json(ACTIVE_SIGNS_URL, openid)

# def getData(openid):
#     raw_data = _get_json(ACTIVE_SIGNS_URL, openid)
#     # 加上下面这两行，把原始数据漂亮地打印在控制台上
#     print("=== 签到接口原始数据 ===")
#     print(json.dumps(raw_data, indent=2, ensure_ascii=False)) 
#     return raw_data


def get_student_profile(openid):
    student_info = _get_json(STUDENT_INFO_URL, openid)
    student_role = _get_json(STUDENT_ROLE_URL, openid)

    if isinstance(student_info, dict) and "message" in student_info:
        return student_info
    if isinstance(student_role, dict) and "message" in student_role:
        return student_role

    profile = {
        "name": None,
        "class_name": None,
        "student_number": None,
        "college_name": None,
        "department_name": None,
    }

    if isinstance(student_info, list) and student_info and isinstance(student_info[0], list):
        first_group = student_info[0]

        if len(first_group) > 2 and isinstance(first_group[2], dict):
            name_value = first_group[2].get("item_value")
            if isinstance(name_value, str) and name_value.strip():
                profile["name"] = name_value.strip()

        for item in first_group:
            if not isinstance(item, dict):
                continue
            item_name = str(item.get("item_name", ""))
            item_value = item.get("item_value")
            if not profile["name"] and isinstance(item_value, str) and "姓名" in item_name:
                profile["name"] = item_value.strip()

    if isinstance(student_role, list) and student_role and isinstance(student_role[0], dict):
        role_data = student_role[0]
        profile["name"] = profile["name"] or role_data.get("name") or role_data.get("item_name")
        profile["class_name"] = role_data.get("class_name")
        profile["student_number"] = role_data.get("student_number")
        profile["college_name"] = role_data.get("college_name")
        profile["department_name"] = role_data.get("department_name")

    if any(profile.values()):
        return profile
    return None


def submit_sign(openid, course_id, sign_id, lat=0.0, lon=0.0):
    payload = {
        "courseId": course_id,
        "signId": sign_id,
        "lat": float(lat),
        "lon": float(lon),
    }
    return _post_json(SIGN_IN_URL, openid, payload)
