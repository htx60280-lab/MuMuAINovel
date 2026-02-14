import React, { useState, useEffect } from 'react';
import { Modal, Checkbox, Button, Space, Tag, Divider, Empty, message } from 'antd';
import {
  EnvironmentOutlined,
  GiftOutlined,
  MinusCircleOutlined,
  TeamOutlined,
  ClockCircleOutlined,
  LineChartOutlined,
  CheckOutlined,
  CloseOutlined,
} from '@ant-design/icons';
import { stateChangeApi } from '../services/api';

interface StateChangeConfirmModalProps {
  visible: boolean;
  chapterId: string;
  chapterNumber: number;
  chapterTitle: string;
  pendingStateChange: {
    items_gained?: string[];
    items_lost?: string[];
    location_change?: { from: string | null; to: string | null };
    relationships?: Record<string, string>;
    status_changes?: Record<string, number>;
    time_passed?: string;
  } | null;
  onConfirm: () => void;
  onReject: () => void;
  onClose: () => void;
}

const StateChangeConfirmModal: React.FC<StateChangeConfirmModalProps> = ({
  visible,
  chapterId,
  chapterNumber,
  chapterTitle,
  pendingStateChange,
  onConfirm,
  onReject,
  onClose,
}) => {
  const [confirming, setConfirming] = useState(false);
  const [rejecting, setRejecting] = useState(false);

  // 各项状态变化的选中状态
  const [selectedItems, setSelectedItems] = useState<{
    items_gained: string[];
    items_lost: string[];
    location_change: boolean;
    relationships: string[];
    status_changes: string[];
    time_passed: boolean;
  }>({
    items_gained: [],
    items_lost: [],
    location_change: false,
    relationships: [],
    status_changes: [],
    time_passed: false,
  });

  // 初始化选中状态（默认全选）
  useEffect(() => {
    if (pendingStateChange) {
      setSelectedItems({
        items_gained: pendingStateChange.items_gained || [],
        items_lost: pendingStateChange.items_lost || [],
        location_change: !!pendingStateChange.location_change?.to,
        relationships: Object.keys(pendingStateChange.relationships || {}),
        status_changes: Object.keys(pendingStateChange.status_changes || {}),
        time_passed: !!pendingStateChange.time_passed,
      });
    }
  }, [pendingStateChange]);

  const handleConfirm = async () => {
    setConfirming(true);
    try {
      // 构建确认的状态变化
      const confirmedChanges: Record<string, unknown> = {};

      if (selectedItems.items_gained.length > 0) {
        confirmedChanges.items_gained = selectedItems.items_gained;
      }
      if (selectedItems.items_lost.length > 0) {
        confirmedChanges.items_lost = selectedItems.items_lost;
      }
      if (selectedItems.location_change && pendingStateChange?.location_change) {
        confirmedChanges.location_change = pendingStateChange.location_change;
      }
      if (selectedItems.relationships.length > 0 && pendingStateChange?.relationships) {
        const selectedRelationships: Record<string, string> = {};
        selectedItems.relationships.forEach((key) => {
          if (pendingStateChange.relationships?.[key]) {
            selectedRelationships[key] = pendingStateChange.relationships[key];
          }
        });
        confirmedChanges.relationships = selectedRelationships;
      }
      if (selectedItems.status_changes.length > 0 && pendingStateChange?.status_changes) {
        const selectedStatusChanges: Record<string, number> = {};
        selectedItems.status_changes.forEach((key) => {
          const value = pendingStateChange.status_changes?.[key];
          if (value !== undefined) {
            selectedStatusChanges[key] = value;
          }
        });
        if (Object.keys(selectedStatusChanges).length > 0) {
          confirmedChanges.status_changes = selectedStatusChanges;
        }
      }
      if (selectedItems.time_passed && pendingStateChange?.time_passed) {
        confirmedChanges.time_passed = pendingStateChange.time_passed;
      }

      await stateChangeApi.confirmStateChange(chapterId, confirmedChanges);
      message.success('状态变化已确认');
      onConfirm();
    } catch (error) {
      message.error('确认失败，请重试');
      console.error('确认状态变化失败:', error);
    } finally {
      setConfirming(false);
    }
  };

  const handleReject = async () => {
    setRejecting(true);
    try {
      await stateChangeApi.rejectStateChange(chapterId);
      message.info('状态变化已拒绝');
      onReject();
    } catch (error) {
      message.error('拒绝失败，请重试');
      console.error('拒绝状态变化失败:', error);
    } finally {
      setRejecting(false);
    }
  };

  const hasAnyChange = pendingStateChange && (
    (pendingStateChange.items_gained?.length || 0) > 0 ||
    (pendingStateChange.items_lost?.length || 0) > 0 ||
    pendingStateChange.location_change?.to ||
    Object.keys(pendingStateChange.relationships || {}).length > 0 ||
    Object.keys(pendingStateChange.status_changes || {}).length > 0 ||
    pendingStateChange.time_passed
  );

  if (!hasAnyChange) {
    return null;
  }

  return (
    <Modal
      title={
        <Space>
          <span>📋 状态变化确认</span>
          <Tag color="blue">第{chapterNumber}章</Tag>
        </Space>
      }
      open={visible}
      onCancel={onClose}
      width={600}
      footer={[
        <Button
          key="reject"
          danger
          icon={<CloseOutlined />}
          onClick={handleReject}
          loading={rejecting}
        >
          全部拒绝
        </Button>,
        <Button
          key="confirm"
          type="primary"
          icon={<CheckOutlined />}
          onClick={handleConfirm}
          loading={confirming}
        >
          确认选中项
        </Button>,
      ]}
    >
      <div style={{ marginBottom: 16 }}>
        <p style={{ color: '#666', marginBottom: 8 }}>
          AI 从《{chapterTitle}》中提取了以下状态变化，请确认是否正确：
        </p>
        <p style={{ color: '#999', fontSize: 12 }}>
          ⚠️ 只有确认的变化才会更新到全局状态，避免 AI 幻觉污染数据
        </p>
      </div>

      {/* 位置变化 */}
      {pendingStateChange?.location_change?.to && (
        <>
          <Divider orientation="left" plain>
            <EnvironmentOutlined /> 位置变化
          </Divider>
          <Checkbox
            checked={selectedItems.location_change}
            onChange={(e) =>
              setSelectedItems((prev) => ({ ...prev, location_change: e.target.checked }))
            }
          >
            <Space>
              <Tag color="orange">{pendingStateChange.location_change.from || '?'}</Tag>
              <span>→</span>
              <Tag color="green">{pendingStateChange.location_change.to}</Tag>
            </Space>
          </Checkbox>
        </>
      )}

      {/* 获得物品 */}
      {(pendingStateChange?.items_gained?.length || 0) > 0 && (
        <>
          <Divider orientation="left" plain>
            <GiftOutlined /> 获得物品
          </Divider>
          <Checkbox.Group
            value={selectedItems.items_gained}
            onChange={(values) =>
              setSelectedItems((prev) => ({ ...prev, items_gained: values as string[] }))
            }
          >
            <Space wrap>
              {pendingStateChange?.items_gained?.map((item) => (
                <Checkbox key={item} value={item}>
                  <Tag color="green">+ {item}</Tag>
                </Checkbox>
              ))}
            </Space>
          </Checkbox.Group>
        </>
      )}

      {/* 失去物品 */}
      {(pendingStateChange?.items_lost?.length || 0) > 0 && (
        <>
          <Divider orientation="left" plain>
            <MinusCircleOutlined /> 失去物品
          </Divider>
          <Checkbox.Group
            value={selectedItems.items_lost}
            onChange={(values) =>
              setSelectedItems((prev) => ({ ...prev, items_lost: values as string[] }))
            }
          >
            <Space wrap>
              {pendingStateChange?.items_lost?.map((item) => (
                <Checkbox key={item} value={item}>
                  <Tag color="red">- {item}</Tag>
                </Checkbox>
              ))}
            </Space>
          </Checkbox.Group>
        </>
      )}

      {/* 状态数值变化 */}
      {Object.keys(pendingStateChange?.status_changes || {}).length > 0 && (
        <>
          <Divider orientation="left" plain>
            <LineChartOutlined /> 状态数值变化
          </Divider>
          <Checkbox.Group
            value={selectedItems.status_changes}
            onChange={(values) =>
              setSelectedItems((prev) => ({ ...prev, status_changes: values as string[] }))
            }
          >
            <Space wrap>
              {Object.entries(pendingStateChange?.status_changes || {}).map(([key, value]) => {
                const displayValue = value > 0 ? `+${value}` : `${value}`;
                const color = value >= 0 ? 'green' : 'red';

                return (
                  <Checkbox key={key} value={key}>
                    <Tag color={color}>
                      {key} {displayValue}
                    </Tag>
                  </Checkbox>
                );
              })}
            </Space>
          </Checkbox.Group>
        </>
      )}

      {/* 关系变化 */}
      {Object.keys(pendingStateChange?.relationships || {}).length > 0 && (
        <>
          <Divider orientation="left" plain>
            <TeamOutlined /> 关系变化
          </Divider>
          <Checkbox.Group
            value={selectedItems.relationships}
            onChange={(values) =>
              setSelectedItems((prev) => ({ ...prev, relationships: values as string[] }))
            }
          >
            <Space direction="vertical" style={{ width: '100%' }}>
              {Object.entries(pendingStateChange?.relationships || {}).map(([char, change]) => (
                <Checkbox key={char} value={char}>
                  <Space>
                    <Tag color="blue">{char}</Tag>
                    <span>{change}</span>
                  </Space>
                </Checkbox>
              ))}
            </Space>
          </Checkbox.Group>
        </>
      )}

      {/* 时间流逝 */}
      {pendingStateChange?.time_passed && (
        <>
          <Divider orientation="left" plain>
            <ClockCircleOutlined /> 时间流逝
          </Divider>
          <Checkbox
            checked={selectedItems.time_passed}
            onChange={(e) =>
              setSelectedItems((prev) => ({ ...prev, time_passed: e.target.checked }))
            }
          >
            <Tag color="purple">{pendingStateChange.time_passed}</Tag>
          </Checkbox>
        </>
      )}

      {!hasAnyChange && (
        <Empty description="本章没有检测到状态变化" />
      )}
    </Modal>
  );
};

export default StateChangeConfirmModal;
