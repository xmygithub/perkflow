from pydantic import BaseModel, ConfigDict, Field


class PhoneRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str = Field(pattern=r"^1[3-9][0-9]{9}$", description="中国大陆 11 位手机号")


class LoginRequest(PhoneRequest):
    code: str = Field(pattern=r"^[0-9]{6}$", description="6 位验证码，保留前导零")


class CurrentUser(BaseModel):
    id: int
    nick_name: str
    icon: str | None = None
    phone_masked: str


class SessionResponse(BaseModel):
    csrf_token: str
    user: CurrentUser | None


class CodeResponse(BaseModel):
    message: str = "模拟验证码已发送，请查看后端终端"
    expires_in: int
    retry_after: int
    sms_mode: str = "console"


class LoginResponse(BaseModel):
    user: CurrentUser
    csrf_token: str
