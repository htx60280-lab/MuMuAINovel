import React, { useState, useEffect } from 'react';
import { Timeline, Card, Tag, Spin, Empty, Space, Select, Typography, Badge } from 'antd';
import {
  StarOutlined,
  BulbOutlined,
  UserAddOutlined,
  EyeOutlined,
  ThunderboltOutlined,
  EnvironmentOutlined,
  GiftOutlined,
  HeartOutlined,
  ClockCircleOutlined,
  CheckCircleOutlined,
} from '@ant-design/icons';
import { keyEventApi } from '../services/api';

const { Text } = Typography;

interface KeyEvent {
  id: string;
  chapter_number: number;
  event_type: string;
  title: string;
  description: string | null;
  importance: number;
  is_resolved: number;
  resolved_chapter: number | null;
  created_at: string;
}

interface KeyEventsTimelineProps {
  projectId: string;
}

// 事件类型配置
const EVENT_TYPE_CONFIG: Record<string, { label: string; icon: React.ReactNode; color: string }> = {
  major_turn: { label: '重大转折', icon: <ThunderboltOutlined />, color: 'red' },
  foreshadow_plant: { label: '伏笔埋入', icon: <BulbOutlined />, color: 'gold' },
  foreshadow_resolve: { label: '伏笔回收', icon: <CheckCircleOutlined />, color: 'green' },
  character_intro: { label: '角色登场', icon: <UserAddOutlined />, color: 'blue' },
  character_death: { label: '角色死亡', icon: <HeartOutlined />, color: 'gray' },
  item_acquire: { label: '获得物品', icon: <GiftOutlined />, color: 'cyan' },
  item_lose: { label: '失去物品', icon: <GiftOutlined />, color: 'orange' },
  relationship_change: { label: '关系变化', icon: <HeartOutlined />, color: 'purple' },
  location_change: { label: '地点变化', icon: <EnvironmentOutlined />, color: 'geekblue' },
  power_up: { label: '实力提升', icon: <StarOutlined />, color: 'volcano' },
  secret_reveal: { label: '秘密揭示', icon: <EyeOutlined />, color: 'magenta' },
};

const KeyEventsTimeline: React.FC<KeyEventsTimelineProps> = ({ projectId }) => {
  const [loading, setLoading] = useState(false);
  const [events, setEvents] = useState<KeyEvent[]>([]);
  const [filterType, setFilterType] = useState<string | undefined>(undefined);
  const [filterImportance, setFilterImportance] = useState<number | undefined>(undefined);

  useEffect(() => {
    if (projectId) {
      loadEvents();
    }
  }, [projectId]);

  const loadEvents = async () => {
    setLoading(true);
    try {
      const data = await keyEventApi.getKeyEvents(projectId);
      setEvents(data.events || []);
    } catch (error) {
      console.error('加载关键事件失败:', error);
    } finally {
      setLoading(false);
    }
  };

  // 过滤事件
  const filteredEvents = events.filter((event) => {
    if (filterType && event.event_type !== filterType) return false;
    if (filterImportance && event.importance < filterImportance) return false;
    return true;
  });

  // 按章节分组
  const groupedEvents = filteredEvents.reduce((acc, event) => {
    const chapter = event.chapter_number;
    if (!acc[chapter]) {
      acc[chapter] = [];
    }
    acc[chapter].push(event);
    return acc;
  }, {} as Record<number, KeyEvent[]>);

  const renderImportanceStars = (importance: number) => {
    return (
      <span style={{ color: '#faad14' }}>
        {'★'.repeat(importance)}
        {'☆'.repeat(5 - importance)}
      </span>
    );
  };

  const renderEventItem = (event: KeyEvent) => {
    const config = EVENT_TYPE_CONFIG[event.event_type] || {
      label: event.event_type,
      icon: <ClockCircleOutlined />,
      color: 'default',
    };

    return (
      <div key={event.id} style={{ marginBottom: 8 }}>
        <Space align="start">
          <Tag color={config.color} icon={config.icon}>
            {config.label}
          </Tag>
          <div>
            <div style={{ fontWeight: 500 }}>
              {event.title}
              {event.is_resolved === 1 && (
                <Badge
                  status="success"
                  text={
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      已回收(第{event.resolved_chapter}章)
                    </Text>
                  }
                  style={{ marginLeft: 8 }}
                />
              )}
            </div>
            {event.description && (
              <Text type="secondary" style={{ fontSize: 13 }}>
                {event.description}
              </Text>
            )}
            <div style={{ marginTop: 4 }}>
              {renderImportanceStars(event.importance)}
            </div>
          </div>
        </Space>
      </div>
    );
  };

  if (loading) {
    return (
      <Card title="📅 关键事件时间轴">
        <div style={{ textAlign: 'center', padding: 40 }}>
          <Spin />
        </div>
      </Card>
    );
  }

  return (
    <Card
      title="📅 关键事件时间轴"
      extra={
        <Space>
          <Select
            placeholder="事件类型"
            allowClear
            style={{ width: 120 }}
            value={filterType}
            onChange={setFilterType}
            options={Object.entries(EVENT_TYPE_CONFIG).map(([key, config]) => ({
              value: key,
              label: config.label,
            }))}
          />
          <Select
            placeholder="重要程度"
            allowClear
            style={{ width: 100 }}
            value={filterImportance}
            onChange={setFilterImportance}
            options={[
              { value: 3, label: '≥3星' },
              { value: 4, label: '≥4星' },
              { value: 5, label: '5星' },
            ]}
          />
        </Space>
      }
    >
      {filteredEvents.length === 0 ? (
        <Empty description="暂无关键事件" />
      ) : (
        <Timeline
          mode="left"
          items={Object.entries(groupedEvents)
            .sort(([a], [b]) => Number(a) - Number(b))
            .map(([chapter, chapterEvents]) => ({
              label: (
                <Text strong style={{ fontSize: 14 }}>
                  第{chapter}章
                </Text>
              ),
              children: (
                <div>
                  {chapterEvents.map((event) => renderEventItem(event))}
                </div>
              ),
            }))}
        />
      )}

      {/* 统计信息 */}
      <div style={{ marginTop: 16, borderTop: '1px solid #f0f0f0', paddingTop: 16 }}>
        <Space split={<span style={{ color: '#d9d9d9' }}>|</span>}>
          <Text type="secondary">共 {events.length} 个事件</Text>
          <Text type="secondary">
            伏笔: {events.filter((e) => e.event_type.includes('foreshadow')).length} 个
          </Text>
          <Text type="secondary">
            转折: {events.filter((e) => e.event_type === 'major_turn').length} 个
          </Text>
          <Text type="secondary">
            已筛选: {filteredEvents.length} 个
          </Text>
        </Space>
      </div>
    </Card>
  );
};

export default KeyEventsTimeline;
