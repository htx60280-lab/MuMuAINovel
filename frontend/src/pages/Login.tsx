import { useEffect, useState } from 'react';
import { Alert, Button, Card, Col, Divider, Form, Input, Layout, Row, Space, Spin, Tag, Typography, message, theme } from 'antd';
import { BookOutlined, LockOutlined, RobotOutlined, SafetyCertificateOutlined, TeamOutlined, ThunderboltOutlined, UserOutlined } from '@ant-design/icons';
import { authApi } from '../services/api';
import { useNavigate, useSearchParams } from 'react-router-dom';
import AnnouncementModal from '../components/AnnouncementModal';

import ThemeSwitch from '../components/ThemeSwitch';

const { Title, Paragraph } = Typography;

export default function Login() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [loading, setLoading] = useState(false);
  const [checking, setChecking] = useState(true);
  const [localAuthEnabled, setLocalAuthEnabled] = useState(false);
  const [linuxdoEnabled, setLinuxdoEnabled] = useState(false);
  const [form] = Form.useForm();
  const { token } = theme.useToken();
  const alphaColor = (color: string, alpha: number) => `color-mix(in srgb, ${color} ${(alpha * 100).toFixed(0)}%, transparent)`;
  const primaryButtonShadow = `0 8px 20px ${alphaColor(token.colorPrimary, 0.28)}`;
  const hoverButtonShadow = `0 12px 28px ${alphaColor(token.colorPrimary, 0.36)}`;
  const [showAnnouncement, setShowAnnouncement] = useState(false);

  // 检查是否已登录和获取认证配置
  useEffect(() => {
    const checkAuth = async () => {
      try {
        await authApi.getCurrentUser();
        // 已登录，重定向到首页
        const redirect = searchParams.get('redirect') || '/';
        navigate(redirect);
      } catch {
        // 未登录，获取认证配置
        try {
          const config = await authApi.getAuthConfig();
          setLocalAuthEnabled(config.local_auth_enabled);
          setLinuxdoEnabled(config.linuxdo_enabled);
        } catch (error) {
          console.error('获取认证配置失败:', error);
          // 默认显示LinuxDO登录
          setLinuxdoEnabled(true);
        }
        setChecking(false);
      }
    };
    checkAuth();
  }, [navigate, searchParams]);

  const handleLocalLogin = async (values: { username: string; password: string }) => {
    try {
      setLoading(true);
      const response = await authApi.localLogin(values.username, values.password);

      if (response.success) {
        message.success('登录成功！');

        // 检查是否永久隐藏公告
        const hideForever = localStorage.getItem('announcement_hide_forever');
        const hideToday = localStorage.getItem('announcement_hide_today');
        const today = new Date().toDateString();

        // 如果永久隐藏或今日已隐藏，则不显示公告
        if (hideForever === 'true' || hideToday === today) {
          const redirect = searchParams.get('redirect') || '/';
          navigate(redirect);
        } else {
          setShowAnnouncement(true);
        }
      }
    } catch (error) {
      console.error('本地登录失败:', error);
      setLoading(false);
    }
  };

  const handleLinuxDOLogin = async () => {
    try {
      setLoading(true);
      const response = await authApi.getLinuxDOAuthUrl();

      // 保存重定向地址到 sessionStorage
      const redirect = searchParams.get('redirect');
      if (redirect) {
        sessionStorage.setItem('login_redirect', redirect);
      }

      // 跳转到 LinuxDO 授权页面
      window.location.href = response.auth_url;
    } catch (error) {
      console.error('获取授权地址失败:', error);
      message.error('获取授权地址失败，请稍后重试');
      setLoading(false);
    }
  };

  if (checking) {
    return (
      <div style={{
        display: 'flex',
        justifyContent: 'center',
        alignItems: 'center',
        minHeight: '100vh',
        background: token.colorBgLayout,
      }}>
        <Spin size="large" style={{ color: token.colorPrimary }} />
      </div>
    );
  }

  // 渲染本地登录表单
  const renderLocalLogin = () => (
    <Form
      form={form}
      layout="vertical"
      onFinish={handleLocalLogin}
      size="large"
      style={{ marginTop: '16px' }}
    >
      <Form.Item
        name="username"
        label="管理账号"
        rules={[{ required: true, message: '请输入管理账号' }]}
      >
        <Input
          prefix={<UserOutlined style={{ color: token.colorTextTertiary }} />}
          placeholder="请输入管理账号"
          autoComplete="username"
          style={{ height: 46, borderRadius: 12 }}
        />
      </Form.Item>
      <Form.Item
        name="password"
        label="访问密钥"
        rules={[{ required: true, message: '请输入访问密钥' }]}
      >
        <Input.Password
          prefix={<LockOutlined style={{ color: token.colorTextTertiary }} />}
          placeholder="请输入访问密钥"
          autoComplete="current-password"
          style={{ height: 46, borderRadius: 12 }}
        />
      </Form.Item>
      <Form.Item style={{ marginBottom: 0, marginTop: 8 }}>
        <Button
          type="primary"
          htmlType="submit"
          loading={loading}
          block
          style={{
            height: 46,
            fontSize: 16,
            fontWeight: 600,
            background: `linear-gradient(90deg, ${token.colorPrimary} 0%, ${alphaColor(token.colorPrimary, 0.86)} 100%)`,
            border: 'none',
            borderRadius: '12px',
            boxShadow: primaryButtonShadow,
          }}
        >
          登录系统
        </Button>
      </Form.Item>
    </Form>
  );

  // 渲染LinuxDO登录
  const renderLinuxDOLogin = () => (
    <div>
      <Button
        type="primary"
        size="large"
        icon={
          <img
            src="/favicon.ico"
            alt="LinuxDO"
            style={{
              width: 20,
              height: 20,
              marginRight: 8,
              verticalAlign: 'middle',
            }}
          />
        }
        loading={loading}
        onClick={handleLinuxDOLogin}
        block
        style={{
          height: 46,
          fontSize: 16,
          fontWeight: 600,
          background: `linear-gradient(90deg, ${token.colorPrimary} 0%, ${alphaColor(token.colorPrimary, 0.86)} 100%)`,
          border: 'none',
          borderRadius: '12px',
          boxShadow: primaryButtonShadow,
          transition: 'all 0.3s ease',
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.transform = 'translateY(-2px)';
          e.currentTarget.style.boxShadow = hoverButtonShadow;
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.transform = 'translateY(0)';
          e.currentTarget.style.boxShadow = primaryButtonShadow;
        }}
      >
        使用 LinuxDO OAuth 登录
      </Button>
    </div>
  );

  const handleAnnouncementClose = () => {
    setShowAnnouncement(false);
    const redirect = searchParams.get('redirect') || '/';
    navigate(redirect);
  };

  const handleDoNotShowToday = () => {
    // 设置今日不再显示
    const today = new Date().toDateString();
    localStorage.setItem('announcement_hide_today', today);
  };

  const handleNeverShow = () => {
    // 设置永久不再显示
    localStorage.setItem('announcement_hide_forever', 'true');
  };

  const loginTips = [
    '本地登录默认账号：admin / admin123',
    '首次 LinuxDO 登录会自动创建账号',
    '系统采用多用户数据隔离机制，每位用户拥有独立的创作空间与配置。',
  ];

  const featureItems = [
    {
      icon: <RobotOutlined />,
      title: '多 AI 模型协同',
      description: '支持 OpenAI、Gemini、Claude 等主流模型，按场景灵活切换。',
    },
    {
      icon: <ThunderboltOutlined />,
      title: '智能向导驱动',
      description: '自动生成大纲、角色与世界观，快速搭建完整故事骨架。',
    },
    {
      icon: <TeamOutlined />,
      title: '角色组织管理',
      description: '人物关系、组织架构可视化管理，复杂设定也能清晰掌控。',
    },
    {
      icon: <BookOutlined />,
      title: '章节创作闭环',
      description: '支持章节生成、编辑、重写与润色，持续提升内容质量。',
    },
  ];

  return (
    <>
      <AnnouncementModal
        visible={showAnnouncement}
        onClose={handleAnnouncementClose}
        onDoNotShowToday={handleDoNotShowToday}
        onNeverShow={handleNeverShow}
      />
      <Layout style={{ minHeight: '100vh', background: token.colorBgLayout }}>
        <div
          style={{
            position: 'fixed',
            top: 20,
            right: 20,
            zIndex: 10,
            padding: '8px 10px',
            borderRadius: 12,
            background: alphaColor(token.colorBgContainer, 0.9),
            border: `1px solid ${token.colorBorderSecondary}`,
            backdropFilter: 'blur(6px)',
          }}
        >
          <ThemeSwitch size="small" />
        </div>
        <Row style={{ minHeight: '100vh' }}>
          <Col xs={0} lg={11}>
            <section
              style={{
                height: '100%',
                padding: '44px 64px 88px',
                display: 'flex',
                flexDirection: 'column',
                justifyContent: 'space-between',
                position: 'relative',
                overflow: 'hidden',
                backgroundColor: alphaColor(token.colorBgContainer, 0.78),
                backgroundImage: `linear-gradient(${alphaColor(token.colorTextSecondary, 0.06)} 1px, transparent 1px), linear-gradient(90deg, ${alphaColor(token.colorTextSecondary, 0.06)} 1px, transparent 1px)`,
                backgroundSize: '68px 68px',
              }}
            >
              <div
                style={{
                  position: 'absolute',
                  inset: 0,
                  background: `radial-gradient(circle at 25% 20%, ${alphaColor(token.colorPrimary, 0.12)} 0%, transparent 50%)`,
                  pointerEvents: 'none',
                }}
              />

              <div
                style={{
                  position: 'relative',
                  zIndex: 1,
                  display: 'flex',
                  flexDirection: 'column',
                  justifyContent: 'space-between',
                  gap: 34,
                  width: '100%',
                  // flex: 1,
                }}
              >
                <Space align="center" size={14}>
                  <div
                    style={{
                      width: 46,
                      height: 46,
                      borderRadius: 14,
                      background: `linear-gradient(135deg, ${token.colorPrimary} 0%, ${alphaColor(token.colorPrimary, 0.7)} 100%)`,
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      boxShadow: primaryButtonShadow,
                    }}
                  >
                    <img
                      src="/logo.svg"
                      alt="MuMuAINovel"
                      style={{ width: 26, height: 26, filter: 'brightness(0) invert(1)' }}
                    />
                  </div>
                  <Title level={3} style={{ margin: 0, color: token.colorText }}>
                    MuMuAINovel
                  </Title>
                </Space>

                <Space direction="vertical" size={32} style={{ width: '100%' }}>
                  <div style={{ maxWidth: 'min(860px, 100%)' }}>
                    <Title
                      level={1}
                      style={{
                        marginBottom: 22,
                        color: token.colorText,
                        lineHeight: 1.12,
                        fontWeight: 800,
                        fontSize: 'clamp(52px, 3vw, 78px)',
                      }}
                    >
                      基于 AI 的
                      <br />
                      <span
                        style={{
                          backgroundImage: `linear-gradient(90deg, ${token.colorPrimary} 0%, #d946ef 100%)`,
                          WebkitBackgroundClip: 'text',
                          backgroundClip: 'text',
                          WebkitTextFillColor: 'transparent',
                          color: token.colorPrimary,
                        }}
                      >
                        智能小说创作助手
                      </span>
                    </Title>
                    <Paragraph
                      style={{
                        fontSize: 'clamp(18px, 1vw, 22px)',
                        lineHeight: 1.85,
                        color: token.colorTextSecondary,
                        marginBottom: 0,
                        maxWidth: 800,
                      }}
                    >
                      从灵感到成稿，围绕「多模型协同、创作流程自动化、角色关系管理、章节精修」构建一体化创作工作台。
                    </Paragraph>
                  </div>

                  <Row gutter={[20, 20]} style={{ width: '100%', maxWidth: 'min(920px, 100%)' }}>
                    {featureItems.map((item) => (
                      <Col span={12} key={item.title}>
                        <Card
                          size="small"
                          bordered={false}
                          style={{
                            height: '100%',
                            minHeight: 120,
                            borderRadius: 16,
                            background: alphaColor(token.colorBgContainer, 0.9),
                          }}
                          bodyStyle={{ padding: 16 }}
                        >
                          <Space direction="vertical" size={8}>
                            <Space size={10} style={{ color: token.colorPrimary, fontWeight: 700, fontSize: 15 }}>
                              {item.icon}
                              <span>{item.title}</span>
                            </Space>
                            <Paragraph style={{ marginBottom: 0, color: token.colorTextSecondary, fontSize: 14, lineHeight: 1.65 }}>
                              {item.description}
                            </Paragraph>
                          </Space>
                        </Card>
                      </Col>
                    ))}
                  </Row>
                </Space>

                <Space size={[10, 14]} wrap style={{ maxWidth: 'min(860px, 100%)' }}>
                  <Tag color="blue">OpenAI</Tag>
                  <Tag color="geekblue">Gemini</Tag>
                  <Tag color="purple">Claude</Tag>
                  <Tag color="cyan">LinuxDO OAuth</Tag>
                  <Tag color="green">Docker Compose</Tag>
                  <Tag color="gold">PostgreSQL</Tag>
                </Space>
              </div>

              <Paragraph
                style={{
                  marginBottom: 0,
                  fontSize: 12,
                  color: token.colorTextTertiary,
                  position: 'relative',
                  zIndex: 1,
                  letterSpacing: 0.4,
                }}
              >
                © 2026 MuMuAINovel · GPLv3 License
              </Paragraph>
            </section>
          </Col>

          <Col xs={24} lg={13}>
            <section
              style={{
                minHeight: '100vh',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                padding: '48px min(7vw, 72px)',
                background: token.colorBgLayout,
              }}
            >
              <div style={{ width: '100%', maxWidth: 480 }}>
                <Space direction="vertical" size={4}>
                  <Title level={2} style={{ marginBottom: 0, fontWeight: 700, color: token.colorText }}>
                    欢迎回来
                  </Title>
                  <Paragraph style={{ marginBottom: 0, color: token.colorTextSecondary }}>
                    登录 MuMuAINovel，继续你的小说创作项目。
                  </Paragraph>
                </Space>

                <div style={{ marginTop: 22 }}>
                  {localAuthEnabled ? renderLocalLogin() : null}

                  {linuxdoEnabled && localAuthEnabled ? (
                    <>
                      <Divider style={{ margin: '18px 0 16px' }}>或</Divider>
                      {renderLinuxDOLogin()}
                    </>
                  ) : null}

                  {!localAuthEnabled && linuxdoEnabled ? renderLinuxDOLogin() : null}

                  {!localAuthEnabled && !linuxdoEnabled ? (
                    <Alert
                      type="warning"
                      showIcon
                      message="当前未启用可用登录方式"
                      description="请联系管理员在系统配置中启用本地登录或 LinuxDO OAuth 登录。"
                    />
                  ) : null}

                  <Divider style={{ margin: '20px 0 14px' }} />
                  <Alert
                    type="info"
                    showIcon
                    icon={<SafetyCertificateOutlined />}
                    style={{ background: alphaColor(token.colorPrimary, 0.06), borderRadius: 12 }}
                    message="登录说明"
                    description={(
                      <ul style={{ margin: 0, paddingLeft: 18 }}>
                        {loginTips.map((tip) => (
                          <li key={tip} style={{ marginBottom: 4 }}>
                            {tip}
                          </li>
                        ))}
                      </ul>
                    )}
                  />
                </div>
              </div>
            </section>
          </Col>
        </Row>
      </Layout>
    </>
  );
}