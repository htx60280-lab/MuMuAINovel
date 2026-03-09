import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, List, Modal, Space, Spin, Tag, Typography, message } from 'antd';
import { FundOutlined, ReloadOutlined } from '@ant-design/icons';

import { consistencyEvaluationApi } from '../services/api';
import type { ConsistencyEvaluationDetail, ConsistencyIssue } from '../types';

const { Paragraph, Text } = Typography;

interface Props {
  projectId: string;
  chapterCount?: number;
}

export default function ConsistencyEvaluationPanel({ projectId, chapterCount = 0 }: Props) {
  const [loading, setLoading] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [latest, setLatest] = useState<ConsistencyEvaluationDetail | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);

  const loadLatest = async () => {
    try {
      setLoading(true);
      const list = await consistencyEvaluationApi.listProjectEvaluations(projectId, 1);
      const latestItem = list.items?.[0];
      if (!latestItem) {
        setLatest(null);
        return;
      }
      const detail = await consistencyEvaluationApi.getEvaluationDetail(latestItem.id);
      setLatest(detail);
    } catch (error) {
      console.error('加载整书一致性评测失败:', error);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (projectId) {
      void loadLatest();
    }
  }, [projectId]);

  const handleRun = async () => {
    try {
      setTriggering(true);
      await consistencyEvaluationApi.createEvaluation(projectId, {
        scope: 'latest_10',
        snapshot_mode: 'latest',
        trigger_type: 'manual',
      });
      message.success('已创建整书一致性评测任务，请稍后刷新查看结果');
      await loadLatest();
    } catch (error) {
      console.error('创建整书一致性评测失败:', error);
      message.error('创建整书一致性评测失败');
    } finally {
      setTriggering(false);
    }
  };

  const categoryTags = useMemo(() => {
    const counts = latest?.summary_json?.category_counts || {};
    return Object.entries(counts);
  }, [latest]);

  const issues = latest?.issues || [];

  return (
    <>
      <Card
        title={<Space><FundOutlined />整书一致性评测</Space>}
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={() => void loadLatest()} loading={loading}>刷新</Button>
            <Button
              type="primary"
              onClick={() => void handleRun()}
              loading={triggering}
              disabled={chapterCount < 1}
            >
              运行评测
            </Button>
          </Space>
        }
        style={{ marginBottom: 16 }}
      >
        {loading ? (
          <div style={{ display: 'flex', justifyContent: 'center', padding: '20px 0' }}>
            <Spin />
          </div>
        ) : !latest ? (
          <Alert
            type="info"
            showIcon
            message="暂无整书一致性评测结果"
            description={chapterCount >= 10 ? '你可以手动运行一次，或等待系统每 10 章自动触发。' : '当前章节数不足 10 章时不会自动触发，但你仍可手动发起评测。'}
          />
        ) : (
          <Space direction="vertical" size={12} style={{ width: '100%' }}>
            <Space wrap>
              <Tag color={latest.status === 'succeeded' ? 'green' : latest.status === 'failed' ? 'red' : 'blue'}>
                状态：{latest.status}
              </Tag>
              <Tag color="gold">总分：{latest.overall_score ?? '--'}</Tag>
              <Tag>问题数：{latest.issue_count}</Tag>
              <Tag>触发：{latest.trigger_type === 'auto' ? `自动（第 ${latest.trigger_chapter_number || '-'} 章）` : '手动'}</Tag>
              <Tag>模型：{latest.model || latest.provider || '默认评测渠道'}</Tag>
            </Space>

            {latest.summary_json?.summary_text ? (
              <Paragraph style={{ marginBottom: 0 }}>{latest.summary_json.summary_text}</Paragraph>
            ) : null}

            {categoryTags.length > 0 ? (
              <Space wrap>
                {categoryTags.map(([name, count]) => (
                  <Tag key={name}>{name}：{count}</Tag>
                ))}
              </Space>
            ) : null}

            {latest.error_message ? (
              <Alert type="error" showIcon message="评测失败" description={latest.error_message} />
            ) : null}

            {issues.length > 0 ? (
              <>
                <List
                  size="small"
                  dataSource={issues.slice(0, 3)}
                  renderItem={(item: ConsistencyIssue) => (
                    <List.Item>
                      <Space direction="vertical" size={2} style={{ width: '100%' }}>
                        <Space wrap>
                          <Tag color="orange">{item.category}</Tag>
                          {item.chapter_number ? <Tag>第 {item.chapter_number} 章</Tag> : null}
                          <Tag>{item.severity}</Tag>
                        </Space>
                        <Text strong>{item.title}</Text>
                        {item.description ? <Text type="secondary">{item.description}</Text> : null}
                      </Space>
                    </List.Item>
                  )}
                />
                <Button type="link" style={{ paddingLeft: 0 }} onClick={() => setDetailOpen(true)}>
                  查看全部问题
                </Button>
              </>
            ) : latest.status === 'succeeded' ? (
              <Alert type="success" showIcon message="最近一次评测未发现明显一致性问题" />
            ) : null}
          </Space>
        )}
      </Card>

      <Modal
        title="整书一致性评测详情"
        open={detailOpen}
        onCancel={() => setDetailOpen(false)}
        footer={null}
        width={920}
      >
        <List
          dataSource={issues}
          locale={{ emptyText: '暂无问题明细' }}
          renderItem={(item: ConsistencyIssue) => (
            <List.Item>
              <Space direction="vertical" size={6} style={{ width: '100%' }}>
                <Space wrap>
                  <Tag color="orange">{item.category}</Tag>
                  {item.subcategory ? <Tag>{item.subcategory}</Tag> : null}
                  <Tag>{item.severity}</Tag>
                  {item.chapter_number ? <Tag>第 {item.chapter_number} 章</Tag> : null}
                  {item.location_text ? <Tag>{item.location_text}</Tag> : null}
                </Space>
                <Text strong>{item.title}</Text>
                {item.description ? <Paragraph style={{ marginBottom: 0 }}>{item.description}</Paragraph> : null}
                {item.exact_quote ? (
                  <Alert type="warning" showIcon message="原文证据" description={item.exact_quote} />
                ) : null}
              </Space>
            </List.Item>
          )}
        />
      </Modal>
    </>
  );
}
