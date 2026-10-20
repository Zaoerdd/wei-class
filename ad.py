import requests
import json

def creatClientId(signId, courseId):
    ws_url = "https://www.teachermate.com.cn/faye"

    post_data = [
    {
        "channel": "/meta/handshake",
        "version": "1.0",
        "supportedConnectionTypes": [
            "websocket",
            "eventsource",
            "long-polling",
            "cross-origin-long-polling",
            "callback-polling"
        ],
        "id": "1"
    }
    ]
    response = requests.post(ws_url, json=post_data, verify=True, timeout=10)
    handshake = json.loads(response.text)
    if not isinstance(handshake, list) or not handshake or not handshake[0].get("clientId"):
        raise RuntimeError(f"Faye 握手失败: {str(handshake)[:200]}")
    clientId = handshake[0]["clientId"]
    signId = signId
    f = [
    {
    "channel": "/meta/connect",
    "clientId": clientId,
    "connectionType": "long-polling",
    "id": "2",
    "advice": {
      "timeout": 0
    }
    },
    {
    "channel": "/meta/subscribe",
    "clientId": clientId,
    "subscription": f"/attendance/{courseId}/{signId}/qr",
    "id": "3"
    }
    ]
    r = requests.post(ws_url, json=f, verify=True, timeout=10)
    j = json.loads(r.text)
    return clientId
if __name__ == '__main__':
    signId = 3815546
    courseId = 1447611
    clientId = creatClientId(signId,courseId)
    print(clientId)
