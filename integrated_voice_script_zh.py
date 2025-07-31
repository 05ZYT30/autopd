import os
import io
import wave
import re
import base64
import requests
from typing import List
from google.cloud import texttospeech
from google.api_core import exceptions
from google.auth import default
import google.auth.transport.requests

PROJECT_ID = "my-ai-250708"
CONSENT_AUDIO_FILE = "/home/molly/podcast/consent_zh.wav"
REFERENCE_AUDIO_FILE = "/home/molly/podcast/reference_zh.wav"
TEXT_TO_READ_FILE = "text_to_read_zh.txt"
SYNTHESIS_OUTPUT_PATH = "streaming_output_zh.wav"
SYNTHESIS_PROGRESS_FILE = "synthesis_progress_zh.txt"
VOICE_CLONING_KEY_FILE = "voice_cloning_key_zh.txt"

def split_text_by_punctuation(text: str) -> List[str]:
    """
    根据英文标点符号分割文本，保留标点，作为自然停顿分段
    """
    # 这里用正则分割，分割点包括 . ? ! ，并保留标点
    pattern = re.compile(r'([^.!?]+[.!?])', re.MULTILINE)
    segments = pattern.findall(text)
    segments = [seg.strip() for seg in segments if seg.strip()]
    # 如果文本末尾无标点，则单独加入
    last_index = sum(len(seg) for seg in segments)
    if last_index < len(text):
        tail = text[last_index:].strip()
        if tail:
            segments.append(tail)
    return segments

def generate_silence(duration_ms: int, sample_rate: int = 24000) -> bytes:
    """
    生成指定毫秒数的单声道16位PCM静音数据。
    """
    num_samples = int(sample_rate * duration_ms / 1000)
    silence = b'\x00\x00' * num_samples
    return silence

def get_next_available_path(base_path):
    if not os.path.exists(base_path):
        return base_path
    base, ext = os.path.splitext(base_path)
    i = 1
    while True:
        new_path = f"{base}_{i}{ext}"
        if not os.path.exists(new_path):
            return new_path
        i += 1

def perform_voice_cloning_streaming_with_pauses(tts_client, voice_cloning_key: str, segments: List[str], output_path: str):
    print(f"正在读取合成进度文件: {SYNTHESIS_PROGRESS_FILE}")
    voice_clone_params = texttospeech.VoiceCloneParams(voice_cloning_key=voice_cloning_key)
    streaming_config = texttospeech.StreamingSynthesizeConfig(
        voice=texttospeech.VoiceSelectionParams(
            language_code="cmn-CN",
            voice_clone=voice_clone_params,
        ),
        streaming_audio_config=texttospeech.StreamingAudioConfig(
            audio_encoding=texttospeech.AudioEncoding.PCM,
            sample_rate_hertz=24000,
        ),
    )
    config_request = texttospeech.StreamingSynthesizeRequest(streaming_config=streaming_config)

    start_index = 0
    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        try:
            with open(SYNTHESIS_PROGRESS_FILE, "r") as f:
                start_index = int(f.read().strip()) + 1
            print(f"从段落 {start_index} 开始继续合成。")
        except Exception:
            print("进度文件读取失败，重头开始合成")

    segments_to_process = segments[start_index:]

    existing_audio = io.BytesIO()
    if os.path.exists(output_path):
        print(f"检测到已有输出音频文件: {output_path}")
        try:
            with wave.open(output_path, 'rb') as w:
                if w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == 24000:
                    existing_audio.write(w.readframes(w.getnframes()))
                else:
                    print("现有音频文件格式不兼容，将重写文件。")
                    existing_audio = io.BytesIO()
            print("成功读取现有音频数据。")
        except Exception as e:
            print(f"读取现有音频文件异常，重写文件: {e}")

    segment_audio = [existing_audio.getvalue()]

    def request_generator():
        yield config_request
        for i, segment_text in enumerate(segments_to_process):
            print(f"合成段 {start_index + i}: {segment_text[:40]}...")
            yield texttospeech.StreamingSynthesizeRequest(
                input=texttospeech.StreamingSynthesisInput(text=segment_text)
            )

    try:
        responses = tts_client.streaming_synthesize(request_generator())
        for i, res in enumerate(responses):
            segment_audio.append(res.audio_content)
            with open(SYNTHESIS_PROGRESS_FILE, "w") as f:
                f.write(str(start_index + i))
    except exceptions.GoogleAPICallError as e:
        print(f"Google API 调用错误: {e}")
        return
    except Exception as e:
        import traceback
        print(f"未知错误发生: {e}")
        traceback.print_exc()
        return

    # 合成所有音频，无额外插入静音（因为标点和语音本身会产生停顿）
    combined_audio = io.BytesIO()
    for seg in segment_audio:
        combined_audio.write(seg)

    try:
        print("正在写入合成后的完整音频...")
        with wave.open(output_path, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(combined_audio.getvalue())
        print("音频写入完成。")
    except Exception as e:
        print(f"写入最终音频文件失败: {e}")
        return

    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        print(f"删除进度记录文件: {SYNTHESIS_PROGRESS_FILE}")
        os.remove(SYNTHESIS_PROGRESS_FILE)
    print(f"最终音频写入: {output_path}")

def load_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

def main():
    print("初始化 TTS 客户端...")
    tts_client = texttospeech.TextToSpeechClient()
    print("TTS 客户端初始化完成。")


    print(f"从文件加载文本: {TEXT_TO_READ_FILE}")
    text = load_text(TEXT_TO_READ_FILE)
    print(f"加载文本成功，长度: {len(text)} 字符。")

    print("正在根据标点符号分段...")
    segments = split_text_by_punctuation(text)
    print(f"分段完成，共 {len(segments)} 段。")
    
    if os.path.exists(VOICE_CLONING_KEY_FILE):
        print(f"检测到已有 voice_cloning_key 文件: {VOICE_CLONING_KEY_FILE}")
        with open(VOICE_CLONING_KEY_FILE, "r") as f:
            voice_cloning_key = f.read().strip()
        print("从文件中读取 voice_cloning_key 成功。")
    else:
        print("未找到现有 voice_cloning_key 文件，开始生成新密钥...")
        voice_cloning_key = create_instant_custom_voice_key(
            project_id=PROJECT_ID,
            reference_audio_path=REFERENCE_AUDIO_FILE,
            consent_audio_path=CONSENT_AUDIO_FILE,
        )
        with open(VOICE_CLONING_KEY_FILE, "w") as f:
            f.write(voice_cloning_key)
        print("voice_cloning_key 生成成功并已保存。")
    
    output_path = get_next_available_path(SYNTHESIS_OUTPUT_PATH)
    print(f"准备写入合成音频文件: {output_path}")
    
    perform_voice_cloning_streaming_with_pauses(
        tts_client=tts_client,
        voice_cloning_key=voice_cloning_key,
        segments=segments,
        output_path=output_path
    )


def create_instant_custom_voice_key(project_id: str, reference_audio_path: str, consent_audio_path: str) -> str:
    print("开始请求 voiceCloningKey API ...")

    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    credentials.refresh(google.auth.transport.requests.Request())
    access_token = credentials.token

    url = "https://texttospeech.googleapis.com/v1beta1/voices:generateVoiceCloningKey"

    def encode_audio(path):
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")

    request_body = {
        "reference_audio": {
            "audio_config": {"audio_encoding": "LINEAR16", "sample_rate_hertz": 24000},
            "content": encode_audio(reference_audio_path),
        },
        "voice_talent_consent": {
            "audio_config": {"audio_encoding": "LINEAR16", "sample_rate_hertz": 24000},
            "content": encode_audio(consent_audio_path),
        },
        "consent_script": "我是此声音的拥有者并授权谷歌使用此声音创建语音合成模型",
        "language_code": "cmn-CN",
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "x-goog-user-project": project_id,
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, json=request_body)
        response.raise_for_status()
        print("voiceCloningKey API 请求成功。")
    except requests.exceptions.HTTPError as http_err:
        print(f"请求失败，状态码: {response.status_code}")
        print("响应内容:")
        try:
            print(response.json())
        except Exception:
            print(response.text)
        raise http_err

    json_resp = response.json()
    if "voiceCloningKey" not in json_resp:
        raise RuntimeError(f"无法获取 voiceCloningKey，API响应: {json_resp}")
    return json_resp["voiceCloningKey"]

if __name__ == "__main__":
    main()

