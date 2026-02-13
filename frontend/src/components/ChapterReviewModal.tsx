/**
 * 章节审查 Modal 组件
 * 显示 AI 深度审查的结果
 */
import React, { useState, useEffect } from 'react';
import {
  Modal,
  Spin,
  Progress,
  Tabs,
  Card,
  Typography,
  List,
  Tag,
  Space,
  Alert,
  Statistic,
  Row,
  Col,
  Button,
} from 'antd';
import {
  UserOutlined,
  LineChartOutlined,
  ThunderboltOutlined,
  CheckCircleOutlined,
  ExclamationCircleOutlined,
  BranchesOutlined,
  SafetyCertificateOutlined,
  ReloadOutlined,
} from '@ant-design/icons';
import { reviewApi } from '../services/api';
import type { ReviewResult, ReviewDimensionResult } from '../types';

interface ChapterReviewModalProps {
  visible: boolean;
  chapterId: string;
  chapterTitle?: string;
  cachedResult?: ReviewResult | null; // 缓存的审查结果
  onClose: () => void;
}

const DIMENSION_CONFIG: Record<string, { name: string; icon: React.ReactNode; color: string }> = {
  ooc: {
    name: '角色一致性',
    icon: <UserOutlined />,
    color: '#1890ff',
  },
  pacing: {
    name: '节奏分析',
    icon: <LineChartOutlined />,
    color: '#52c41a',
  },
  high_point: {
    name: '爽点密度',
    icon: <ThunderboltOutlined />,
    color: '#faad14',
  },
  three_line_rhythm: {
    name: '三线节奏',
    icon: <BranchesOutlined />,
    color: '#722ed1',
  },
  consistency: {
    name: '设定一致性',
    icon: <SafetyCertificateOutlined />,
    color: '#13c2c2',
  },
};

const getScoreColor = (score: number): string => {
  if (score >= 90) return '#52c41a';
  if (score >= 70) return '#1890ff';
  if (score >= 50) return '#faad14';
  return '#ff4d4f';
};

const getScoreLevel = (score: number): string => {
  if (score >= 90) return '优秀';
  if (score >= 70) return '良好';
  if (score >= 50) return '一般';
  return '需改进';
};

const normalizeSuggestionText = (item: unknown): string => {
  if (typeof item === 'string') {
    return item;
  }
  if (item && typeof item === 'object') {
    const record = item as Record<string, unknown>;
    if (typeof record.suggestion === 'string') {
      return record.suggestion;
    }
    if (typeof record.content === 'string') {
      return record.content;
    }
    if (typeof record.text === 'string') {
      return record.text;
    }
  }
  try {
    return JSON.stringify(item);
  } catch {
    return String(item ?? '');
  }
};

const normalizeAnalysisText = (value: unknown): string => {
  if (typeof value === 'string') {
    return value;
  }
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    if (typeof record.analysis === 'string') {
      return record.analysis;
    }
    if (typeof record.text === 'string') {
      return record.text;
    }
    if (typeof record.suggestion === 'string') {
      return record.suggestion;
    }
  }
  try {
    return JSON.stringify(value);
  } catch {
    return String(value ?? '');
  }
};

const normalizeReviewResult = (data: ReviewResult): ReviewResult => {
  const normalizedDimensions: Record<string, ReviewDimensionResult> = {};

  Object.entries(data.dimensions || {}).forEach(([key, value]) => {
    const analysis = normalizeAnalysisText(value.analysis);
    const suggestions = Array.isArray(value.suggestions)
      ? value.suggestions.map(normalizeSuggestionText).filter((item) => item.trim().length > 0)
      : [];

    normalizedDimensions[key] = {
      ...value,
      analysis,
      suggestions,
    };
  });

  return {
    ...data,
    dimensions: normalizedDimensions,
  };
};

const coercePercent = (value: unknown): number => {
  if (typeof value === 'number') {
    return Math.max(0, Math.min(100, value));
  }
  if (typeof value !== 'string') {
    return 0;
  }
  const match = value.match(/-?\d+(?:[.,]\d+)?/);
  if (!match) {
    return 0;
  }
  const raw = match[0];
  let parsed: number | null = null;
  if (raw.includes(',') && !raw.includes('.')) {
    const candidate = Number(raw.replace(',', '.'));
    if (!Number.isNaN(candidate) && candidate >= 0 && candidate <= 100) {
      parsed = candidate;
    }
  }
  if (parsed === null) {
    const candidate = Number(raw.replace(/,/g, ''));
    if (!Number.isNaN(candidate)) {
      parsed = candidate;
    }
  }
  if (parsed === null) {
    return 0;
  }
  return Math.max(0, Math.min(100, parsed));
};

const normalizeLineDensity = (details: Record<string, unknown>) => {
  const raw = (details.line_density || details.lineDensity || details.lines || details.line) as Record<string, unknown> | undefined;
  const source = raw && typeof raw === 'object' ? raw : (details as Record<string, unknown>);

  const pick = (keys: string[]) => {
    for (const key of keys) {
      const value = (source as Record<string, unknown>)[key];
      if (value && typeof value === 'object') {
        return value as Record<string, unknown>;
      }
      if (value !== undefined && value !== null) {
        return { percentage: value } as Record<string, unknown>;
      }
    }
    return {} as Record<string, unknown>;
  };

  return {
    plot: pick(['plot', 'plot_line', 'plotLine', 'story', 'story_line', '情节线', '剧情线', '主线']),
    character: pick(['character', 'character_line', 'characterLine', '人物线', '角色线']),
    world: pick(['world', 'world_line', 'worldLine', '世界线', '环境线']),
  };
};

const ChapterReviewModal: React.FC<ChapterReviewModalProps> = ({
  visible,
  chapterId,
  chapterTitle,
  cachedResult,
  onClose,
}) => {
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<ReviewResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [fromCache, setFromCache] = useState(false);

  useEffect(() => {
    if (visible && chapterId) {
      // 如果有缓存结果，先校验是否完整
      if (cachedResult) {
        const threeLine = cachedResult.dimensions?.three_line_rhythm;
        const hasThreeLine = threeLine && typeof threeLine === 'object';
        const details = hasThreeLine && threeLine.details && typeof threeLine.details === 'object'
          ? (threeLine.details as Record<string, unknown>)
          : null;
        const hasLineDensity = !!details && (
          'line_density' in details ||
          'lineDensity' in details ||
          'plot' in details ||
          'character' in details ||
          'world' in details
        );
        const hasAnalysis = hasThreeLine && typeof threeLine.analysis === 'string' && threeLine.analysis.trim().length > 0;

        if (hasThreeLine && hasLineDensity && hasAnalysis) {
          const normalizedCached = normalizeReviewResult(cachedResult);
          const normalizedLineDensity = normalizeLineDensity(
            (normalizedCached.dimensions.three_line_rhythm?.details || {}) as Record<string, unknown>
          ) as Record<string, { percentage?: number }>;
          const plotPercent = coercePercent(normalizedLineDensity.plot?.percentage);
          const characterPercent = coercePercent(normalizedLineDensity.character?.percentage);
          const worldPercent = coercePercent(normalizedLineDensity.world?.percentage);
          const allZero = plotPercent === 0 && characterPercent === 0 && worldPercent === 0;

          if (allZero) {
            fetchReview({ force_refresh: true });
            return;
          }

          setResult(normalizeReviewResult(cachedResult));
          setFromCache(true);
          setLoading(false);
          setError(null);
        } else {
          // 缓存缺字段，强制重新审查
          fetchReview({ force_refresh: true });
        }
      } else {
        // 否则请求API（API会返回缓存或新审查结果）
        fetchReview();
      }
    }
  }, [visible, chapterId, cachedResult]);

  const fetchReview = async (options?: { force_refresh?: boolean }) => {
    setLoading(true);
    setError(null);
    setResult(null);
    setFromCache(false);

    try {
      const data = await reviewApi.reviewChapter(chapterId, options);
      setResult(normalizeReviewResult(data));
      // 检查是否来自缓存
      if (data.metadata?.from_cache) {
        setFromCache(true);
      }
    } catch (err: unknown) {
      const error = err as Error;
      setError(error.message || '审查失败，请重试');
    } finally {
      setLoading(false);
    }
  };

  // 强制重新审查
  const handleForceRefresh = async () => {
    setLoading(true);
    setError(null);
    setResult(null);
    setFromCache(false);

    try {
      await fetchReview({ force_refresh: true });
    } catch (err: unknown) {
      const error = err as Error;
      setError(error.message || '审查失败，请重试');
    } finally {
      setLoading(false);
    }
  };

  const renderThreeLineRhythm = (dimResult: ReviewDimensionResult) => {
    const details = (dimResult.details && typeof dimResult.details === 'object' ? dimResult.details : {}) as Record<string, unknown>;
    const lineDensity = normalizeLineDensity(details) as Record<string, { percentage?: number; momentum?: string; depth?: string; immersion?: string }>;
    const balanceAssessment = (details.balance_assessment || {}) as {
      is_balanced?: boolean;
      dominant_line?: string;
      weak_line?: string;
      recommendation?: string;
    };

    const plotData = lineDensity.plot || { percentage: 0 };
    const characterData = lineDensity.character || { percentage: 0 };
    const worldData = lineDensity.world || { percentage: 0 };
    plotData.percentage = coercePercent(plotData.percentage);
    characterData.percentage = coercePercent(characterData.percentage);
    worldData.percentage = coercePercent(worldData.percentage);

    return (
      <div style={{ padding: '16px 0' }}>
        <Row gutter={16} style={{ marginBottom: 24 }}>
          <Col span={8}>
            <Card size="small">
              <Statistic
                title="维度评分"
                value={dimResult.score}
                suffix="/ 100"
                valueStyle={{ color: getScoreColor(dimResult.score) }}
              />
            </Card>
          </Col>
          <Col span={16}>
            <Card size="small">
              <Typography.Text strong>节奏模式：</Typography.Text>
              <Tag color="#722ed1" style={{ marginLeft: 8 }}>
                {(details.rhythm_pattern as string) || '未知'}
              </Tag>
            </Card>
          </Col>
        </Row>

        {/* 三线密度可视化 */}
        <Card title="三线密度分布" size="small" style={{ marginBottom: 16 }}>
          <div style={{ marginBottom: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
              <Typography.Text style={{ width: 80 }}>情节线</Typography.Text>
              <Progress
                percent={plotData.percentage || 0}
                strokeColor="#ff4d4f"
                style={{ flex: 1, marginRight: 8 }}
                format={(p) => `${p}%`}
              />
              <Tag color="red">{plotData.momentum || '-'}</Tag>
            </div>
            <div style={{ display: 'flex', alignItems: 'center', marginBottom: 8 }}>
              <Typography.Text style={{ width: 80 }}>人物线</Typography.Text>
              <Progress
                percent={characterData.percentage || 0}
                strokeColor="#1890ff"
                style={{ flex: 1, marginRight: 8 }}
                format={(p) => `${p}%`}
              />
              <Tag color="blue">{characterData.depth || '-'}</Tag>
            </div>
            <div style={{ display: 'flex', alignItems: 'center' }}>
              <Typography.Text style={{ width: 80 }}>世界线</Typography.Text>
              <Progress
                percent={worldData.percentage || 0}
                strokeColor="#52c41a"
                style={{ flex: 1, marginRight: 8 }}
                format={(p) => `${p}%`}
              />
              <Tag color="green">{worldData.immersion || '-'}</Tag>
            </div>
          </div>
        </Card>

        {/* 平衡性评估 */}
        <Card title="平衡性评估" size="small" style={{ marginBottom: 16 }}>
          <Space direction="vertical" style={{ width: '100%' }}>
            <div>
              <Typography.Text strong>平衡状态：</Typography.Text>
              <Tag color={balanceAssessment.is_balanced ? 'green' : 'orange'} style={{ marginLeft: 8 }}>
                {balanceAssessment.is_balanced ? '平衡' : '失衡'}
              </Tag>
            </div>
            {balanceAssessment.dominant_line && balanceAssessment.dominant_line !== 'balanced' && (
              <div>
                <Typography.Text strong>主导线：</Typography.Text>
                <Tag style={{ marginLeft: 8 }}>
                  {balanceAssessment.dominant_line === 'plot' ? '情节线' :
                   balanceAssessment.dominant_line === 'character' ? '人物线' : '世界线'}
                </Tag>
              </div>
            )}
            {balanceAssessment.weak_line && balanceAssessment.weak_line !== 'none' && (
              <div>
                <Typography.Text strong>薄弱线：</Typography.Text>
                <Tag color="warning" style={{ marginLeft: 8 }}>
                  {balanceAssessment.weak_line === 'plot' ? '情节线' :
                   balanceAssessment.weak_line === 'character' ? '人物线' : '世界线'}
                </Tag>
              </div>
            )}
            {balanceAssessment.recommendation && (
              <Typography.Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0 }}>
                {balanceAssessment.recommendation}
              </Typography.Paragraph>
            )}
          </Space>
        </Card>

        <Card title="分析结果" size="small" style={{ marginBottom: 16 }}>
          <Typography.Paragraph>{dimResult.analysis || '暂无分析内容'}</Typography.Paragraph>
        </Card>

        {dimResult.suggestions && dimResult.suggestions.length > 0 && (
          <Card title="改进建议" size="small">
            <List
              size="small"
              dataSource={dimResult.suggestions}
              renderItem={(item, index) => (
                <List.Item>
                  <Space>
                    <Tag color="purple">{index + 1}</Tag>
                    <Typography.Text>{normalizeSuggestionText(item)}</Typography.Text>
                  </Space>
                </List.Item>
              )}
            />
          </Card>
        )}
      </div>
    );
  };

  const renderDimensionTab = (dimKey: string, dimResult: ReviewDimensionResult) => {
    // 三线节奏使用特殊渲染
    if (dimKey === 'three_line_rhythm') {
      return renderThreeLineRhythm(dimResult);
    }

    // 获取维度配置（用于未来扩展）
    const _config = DIMENSION_CONFIG[dimKey] || {
      name: dimKey,
      icon: <CheckCircleOutlined />,
      color: '#1890ff',
    };
    void _config; // 标记为有意未使用

    return (
      <div style={{ padding: '16px 0' }}>
        <Row gutter={16} style={{ marginBottom: 24 }}>
          <Col span={8}>
            <Card size="small">
              <Statistic
                title="维度评分"
                value={dimResult.score}
                suffix="/ 100"
                valueStyle={{ color: getScoreColor(dimResult.score) }}
              />
            </Card>
          </Col>
          <Col span={16}>
            <Card size="small">
              <Typography.Text strong>评级：</Typography.Text>
              <Tag color={getScoreColor(dimResult.score)} style={{ marginLeft: 8 }}>
                {getScoreLevel(dimResult.score)}
              </Tag>
            </Card>
          </Col>
        </Row>

        <Card title="分析结果" size="small" style={{ marginBottom: 16 }}>
          <Typography.Paragraph>{dimResult.analysis || '暂无分析内容'}</Typography.Paragraph>
        </Card>

        {dimResult.suggestions && dimResult.suggestions.length > 0 && (
          <Card title="改进建议" size="small">
            <List
              size="small"
              dataSource={dimResult.suggestions}
              renderItem={(item, index) => (
                <List.Item>
                  <Space>
                    <Tag color="blue">{index + 1}</Tag>
                    <Typography.Text>{normalizeSuggestionText(item)}</Typography.Text>
                  </Space>
                </List.Item>
              )}
            />
          </Card>
        )}
      </div>
    );
  };

  const tabItems = result
    ? Object.entries(result.dimensions).map(([key, value]) => {
        const config = DIMENSION_CONFIG[key] || { name: key, icon: null, color: '#1890ff' };
        return {
          key,
          label: (
            <Space>
              {config.icon}
              {config.name}
              <Tag color={getScoreColor(value.score)}>{value.score}</Tag>
            </Space>
          ),
          children: renderDimensionTab(key, value),
        };
      })
    : [];

  return (
    <Modal
      title={`AI 深度审查 - ${chapterTitle || '章节'}`}
      open={visible}
      onCancel={onClose}
      footer={null}
      width={700}
      destroyOnClose
    >
      {loading && (
        <div style={{ textAlign: 'center', padding: '60px 0' }}>
          <Spin size="large" />
          <Typography.Paragraph style={{ marginTop: 16 }}>
            正在进行 AI 深度审查，请稍候...
          </Typography.Paragraph>
          <Typography.Text type="secondary">
            审查包含角色一致性、节奏分析、爽点密度、三线节奏四个维度
          </Typography.Text>
        </div>
      )}

      {error && (
        <Alert
          message="审查失败"
          description={error}
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
        />
      )}

      {result && !loading && (
        <>
          {/* 缓存提示 */}
          {fromCache && (
            <Alert
              message="已加载缓存的审查结果"
              description="此结果来自章节生成时的自动审查。点击下方按钮可重新审查。"
              type="info"
              showIcon
              style={{ marginBottom: 16 }}
              action={
                <Button
                  size="small"
                  icon={<ReloadOutlined />}
                  onClick={handleForceRefresh}
                >
                  重新审查
                </Button>
              }
            />
          )}


          {!fromCache && (
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 16 }}>
              <Button
                size="small"
                icon={<ReloadOutlined />}
                onClick={handleForceRefresh}
              >
                重新审查
              </Button>
            </div>
          )}

          {/* 综合评分 */}
          <Card style={{ marginBottom: 16, textAlign: 'center' }}>
            <Progress
              type="circle"
              percent={result.overall_score}
              format={(percent) => (
                <div>
                  <div style={{ fontSize: 24, fontWeight: 'bold' }}>{percent?.toFixed(1)}</div>
                  <div style={{ fontSize: 12, color: '#999' }}>综合评分</div>
                </div>
              )}
              strokeColor={getScoreColor(result.overall_score)}
              size={120}
            />
            <div style={{ marginTop: 16 }}>
              <Tag
                icon={
                  result.overall_score >= 70 ? (
                    <CheckCircleOutlined />
                  ) : (
                    <ExclamationCircleOutlined />
                  )
                }
                color={getScoreColor(result.overall_score)}
              >
                {getScoreLevel(result.overall_score)}
              </Tag>
            </div>
          </Card>

          {/* 各维度详情 */}
          <Tabs items={tabItems} />

          {/* 元数据 */}
          {result.metadata && (
            <Typography.Text type="secondary" style={{ display: 'block', marginTop: 16 }}>
              审查时间: {result.reviewed_at}
              {typeof result.metadata.word_count === 'number' ? ` | 章节字数: ${result.metadata.word_count}` : ''}
            </Typography.Text>
          )}
        </>
      )}
    </Modal>
  );
};

export default ChapterReviewModal;
