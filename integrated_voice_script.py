import requests
import os
import json
import base64
import io
import wave
from google.cloud import texttospeech
from google.cloud.texttospeech_v1beta1.services.text_to_speech import client
from google.api_core import exceptions

YOUR_PROJECT_ID = "my-ai-250708"

CONSENT_AUDIO_FILE = "/home/molly/podcast/consent.wav"
REFERENCE_AUDIO_FILE = "/home/molly/podcast/reference.wav"


VOICE_CLONING_KEY_FILE = "voice_cloning_key.txt"
ACCESS_TOKEN_FILE = "access_token.txt"
TEXT_TO_READ_FILE = "text_to_read.txt"
SYNTHESIS_PROGRESS_FILE = "synthesis_progress.txt"


SYNTHESIS_OUTPUT_PATH = "streaming_output.wav"


def get_access_token() -> str | None:

    access_token = os.getenv("GOOGLE_ACCESS_TOKEN")
    if access_token:
        print("Access Token 已从环境变量 'GOOGLE_ACCESS_TOKEN' 获取。")
        return access_token


    if os.path.exists(ACCESS_TOKEN_FILE):
        try:
            with open(ACCESS_TOKEN_FILE, "r") as f:
                token_from_file = f.read().strip()
            if token_from_file:
                print(f"Access Token 已从文件 '{ACCESS_TOKEN_FILE}' 获取。")
                return token_from_file
        except Exception as e:
            print(f"警告: 无法从文件 '{ACCESS_TOKEN_FILE}' 读取 Access Token: {e}")

    return None


def create_instant_custom_voice_key(
    access_token: str,
    project_id: str,
    reference_audio_bytes: bytes,
    consent_audio_bytes: bytes
) -> str | None:
    """
    通过 Google Cloud Text-to-Speech API 生成即时自定义语音的语音克隆密钥。
    """
    url = "https://texttospeech.googleapis.com/v1beta1/voices:generateVoiceCloningKey"

    reference_audio_b64 = base64.b64encode(reference_audio_bytes).decode('utf-8')
    consent_audio_b64 = base64.b64encode(consent_audio_bytes).decode('utf-8')

    request_body = {
        "reference_audio": {
            "audio_config": {"audio_encoding": "LINEAR16", "sample_rate_hertz": 24000},
            "content": reference_audio_b64,
        },
        "voice_talent_consent": {
            "audio_config": {"audio_encoding": "LINEAR16", "sample_rate_hertz": 24000},
            "content": consent_audio_b64,
        },
        "consent_script": "I am the owner of this voice and I consent to Google using this voice to create a synthetic voice model.",
        "language_code": "en-US",
    }

    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "x-goog-user-project": project_id,
            "Content-Type": "application/json; charset=utf-8",
        }

        print(f"正在发送请求到: {url}")
        response = requests.post(url, headers=headers, json=request_body)
        response.raise_for_status()

        response_json = response.json()
        voice_cloning_key = response_json.get("voiceCloningKey")

        if voice_cloning_key:
            print("\n" + "="*50)
            print("成功生成语音克隆密钥！请妥善保存此密钥：")
            print(voice_cloning_key)
            print("="*50 + "\n")
            return voice_cloning_key
        else:
            print(f"API 响应中未找到 'voiceCloningKey' 字段。完整响应:\n{json.dumps(response_json, indent=2)}")
            return None

    except requests.exceptions.HTTPError as http_err:
        print(f"HTTP 错误发生: {http_err}")
        if response is not None:
            print(f"API 响应状态码: {response.status_code}")
            print(f"API 响应内容:\n{response.text}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"发送请求时发生网络或连接错误: {e}")
        return None
    except json.JSONDecodeError as e:
        print(f"错误：无法解码 API 响应为 JSON 格式: {e}")
        return None
    except Exception as e:
        print(f"发生未知错误: {e}")
        return None


def perform_voice_cloning_with_simulated_streaming(
    voice_cloning_key: str,
    simulated_streamed_text: list[str],
    language_code: str,
    synthesis_output_path: str,
    tts_client: client.TextToSpeechClient,
) -> None:
    """
    执行语音克隆流式合成，支持断点续传。
    """
    voice_clone_params = texttospeech.VoiceCloneParams(
        voice_cloning_key=voice_cloning_key
    )
    streaming_config = texttospeech.StreamingSynthesizeConfig(
        voice=texttospeech.VoiceSelectionParams(
            language_code=language_code, voice_clone=voice_clone_params
        ),
        streaming_audio_config=texttospeech.StreamingAudioConfig(
            audio_encoding=texttospeech.AudioEncoding.PCM,
            sample_rate_hertz=24000,
        ),
    )
    config_request = texttospeech.StreamingSynthesizeRequest(
        streaming_config=streaming_config
    )


    start_index = 0
    if os.path.exists(SYNTHESIS_PROGRESS_FILE):
        try:
            with open(SYNTHESIS_PROGRESS_FILE, "r") as f:
                last_completed_index = int(f.read().strip())
                start_index = last_completed_index + 1
                print(f"检测到上次合成中断，将从原始文本索引 {start_index} 开始续传。")
        except ValueError:
            print(f"进度文件 '{SYNTHESIS_PROGRESS_FILE}' 内容无效，将从头开始。")

            os.remove(SYNTHESIS_PROGRESS_FILE)
        except Exception as e:
            print(f"读取进度文件时出错: {e}，将从头开始。")


    if start_index >= len(simulated_streamed_text):
        print("所有文本块已成功合成，无需续传。")

        if os.path.exists(SYNTHESIS_PROGRESS_FILE):
            os.remove(SYNTHESIS_PROGRESS_FILE)
        return


    remaining_text_chunks = simulated_streamed_text[start_index:]


    existing_audio_buffer = io.BytesIO()
    if os.path.exists(synthesis_output_path):
        try:
            with wave.open(synthesis_output_path, 'rb') as existing_wav:

                if existing_wav.getnchannels() == 1 and \
                   existing_wav.getsampwidth() == 2 and \
                   existing_wav.getframerate() == 24000:
                    existing_audio_buffer.write(existing_wav.readframes(existing_wav.getnframes()))
                    print(f"已加载现有音频文件 '{synthesis_output_path}' 的内容。")
                else:
                    print(f"警告: 现有音频文件 '{synthesis_output_path}' 格式不兼容，将重新生成。")
        except wave.Error as we:
            print(f"读取现有音频文件 '{synthesis_output_path}' 时出现 Wave 错误: {we}，将重新生成。")
        except Exception as e:
            print(f"读取现有音频文件 '{synthesis_output_path}' 时发生未知错误: {e} ，将重新生成。")

    segment_audio_contents = [existing_audio_buffer.getvalue()]


    def request_generator_with_resume():
        yield config_request
        for i, text in enumerate(remaining_text_chunks):
            current_original_index = start_index + i
            print(f"正在处理文本块 (原始索引: {current_original_index}/{len(simulated_streamed_text)-1}): '{text[:50]}...'")
            yield texttospeech.StreamingSynthesizeRequest(
                input=texttospeech.StreamingSynthesisInput(text=text)
            )

    try:
        streaming_responses = tts_client.streaming_synthesize(request_generator_with_resume())

        for i, response in enumerate(streaming_responses):
            audio_content = response.audio_content

            segment_audio_contents.append(audio_content)


            current_original_index = start_index + i
            with open(SYNTHESIS_PROGRESS_FILE, "w") as f:
                f.write(str(current_original_index))


    except exceptions.GoogleAPICallError as e:
        print(f"Google API 调用错误发生: {e}")
        print("合成中断。请检查网络连接或 API 限制。")

    except Exception as e:
        print(f"流式合成过程中发生未知错误: {e}")
        print("合成中断。")


    finally:

        if segment_audio_contents:
            final_audio_buffer = io.BytesIO()
            for audio_segment in segment_audio_contents:
                final_audio_buffer.write(audio_segment)

            try:
                with wave.open(synthesis_output_path, 'wb') as wav_file:
                    wav_file.setnchannels(1)
                    wav_file.setsampwidth(2)
                    wav_file.setframerate(24000)
                    wav_file.writeframes(final_audio_buffer.getvalue())
                print(f'所有已收集的音频内容已写入文件: {synthesis_output_path}.')
            except Exception as write_err:
                print(f"写入最终音频文件时发生错误: {write_err}")
        else:
            print("没有收集到任何音频内容。")


    if start_index + len(remaining_text_chunks) >= len(simulated_streamed_text) and \
       os.path.exists(SYNTHESIS_PROGRESS_FILE):
        os.remove(SYNTHESIS_PROGRESS_FILE)
        print(f"合成完成，进度文件 '{SYNTHESIS_PROGRESS_FILE}' 已删除。")



def main():

    access_token = get_access_token()
    if not access_token:
        print("错误: 未能获取到 Access Token。请确保环境变量 'GOOGLE_ACCESS_TOKEN' 已设置或 'access_token.txt' 文件存在并包含有效令牌。")
        return


    if not os.path.exists(CONSENT_AUDIO_FILE):
        print(f"错误: 同意声明文件 '{CONSENT_AUDIO_FILE}' 不存在。")
        return
    if not os.path.exists(REFERENCE_AUDIO_FILE):
        print(f"错误: 参考音频文件 '{REFERENCE_AUDIO_FILE}' 不存在。")
        return


    try:
        with io.open(CONSENT_AUDIO_FILE, "rb") as f:
            consent_audio_bytes = f.read()
        print(f"已读取同意声明文件: {CONSENT_AUDIO_FILE}")

        with io.open(REFERENCE_AUDIO_FILE, "rb") as f:
            reference_audio_bytes = f.read()
        print(f"已读取参考音频文件: {REFERENCE_AUDIO_FILE}")
    except Exception as e:
        print(f"读取音频文件时出错: {e}")
        return


    voice_cloning_key = None
    if os.path.exists(VOICE_CLONING_KEY_FILE):
        try:
            with open(VOICE_CLONING_KEY_FILE, "r") as f:
                voice_cloning_key = f.read().strip()
            print(f"语音克隆密钥已从文件 '{VOICE_CLONING_KEY_FILE}' 加载。")
        except Exception as e:
            print(f"警告: 无法从文件 '{VOICE_CLONING_KEY_FILE}' 读取语音克隆密钥: {e}")

    if not voice_cloning_key:
        print("正在生成新的语音克隆密钥...")
        generated_key = create_instant_custom_voice_key(
            access_token,
            YOUR_PROJECT_ID,
            reference_audio_bytes,
            consent_audio_bytes
        )
        if generated_key:
            voice_cloning_key = generated_key
            try:
                with open(VOICE_CLONING_KEY_FILE, "w") as f:
                    f.write(generated_key)
                print(f"新生成的语音克隆密钥已成功保存到文件: {VOICE_CLONING_KEY_FILE}")
            except Exception as e:
                print(f"错误: 无法将密钥保存到文件 {VOICE_CLONING_KEY_FILE}: {e}")
        else:
            print("错误: 无法生成语音克隆密钥。程序退出。")
            return


    if not os.path.exists(TEXT_TO_READ_FILE):
        print(f"错误: 朗读内容文件 '{TEXT_TO_READ_FILE}' 不存在。")
        return

    try:
        with open(TEXT_TO_READ_FILE, "r", encoding="utf-8") as f:
            text_content = f.read()

        simulated_streamed_text = [text_content.strip()]

        print(f"已从文件 '{TEXT_TO_READ_FILE}' 读取朗读内容，并分割成 {len(simulated_streamed_text)} 个文本块。")
    except Exception as e:
        print(f"读取朗读内容文件时出错: {e}")
        return


    if voice_cloning_key and simulated_streamed_text:
        tts_client = texttospeech.TextToSpeechClient()
        perform_voice_cloning_with_simulated_streaming(
            voice_cloning_key=voice_cloning_key,
            simulated_streamed_text=simulated_streamed_text,
            language_code='en-US',
            synthesis_output_path=SYNTHESIS_OUTPUT_PATH,
            tts_client=tts_client,
        )
    else:
        print("未能执行语音合成，因为缺少语音克隆密钥或朗读内容。")

if __name__ == "__main__":
    main()

