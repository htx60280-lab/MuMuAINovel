import React, { useState, useEffect } from 'react';
import { Modal, Input, Button, Space, Tag, message, Popconfirm } from 'antd';
import { PlusOutlined, DeleteOutlined } from '@ant-design/icons';

const HIDDEN_WORLD_STATE_KEYS = new Set(['_state_hidden_keys']);

const syncHiddenKeys = (state: Record<string, any>, predicate: (item: string) => boolean) => {
  if (!Array.isArray(state._state_hidden_keys)) {
    return state._state_hidden_keys;
  }
  return state._state_hidden_keys.filter((item: string) => !predicate(item));
};

const normalizeStateKey = (key: string) => {
  const trimmed = key.trim();
  const aliases: Record<string, string> = {
    cultivation_level: '修为',
    realm: '修为',
    stage: '修为',
    主角修为: '修为',
    主角境界: '修为',
    wealth: '财富',
    money: '财富',
    gold: '财富',
    生命值: 'hp',
    血量: 'hp',
    气血: 'hp',
    health: 'hp',
    health_points: 'hp',
  };

  return aliases[trimmed] || aliases[trimmed.toLowerCase()] || trimmed;
};

interface WorldStateEditorProps {
  visible: boolean;
  worldState: Record<string, any>;
  onSave: (newState: Record<string, any>) => Promise<void>;
  onCancel: () => void;
}

const WorldStateEditor: React.FC<WorldStateEditorProps> = ({
  visible,
  worldState,
  onSave,
  onCancel,
}) => {
  const [editingState, setEditingState] = useState<Record<string, any>>({});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (visible) {
      setEditingState(JSON.parse(JSON.stringify(worldState || {})));
    }
  }, [visible, worldState]);

  const handleSave = async () => {
    try {
      setSaving(true);
      await onSave(editingState);
      message.success('世界状态已更新');
      onCancel();
    } catch (error) {
      message.error('保存失败，请重试');
      console.error('保存世界状态失败:', error);
    } finally {
      setSaving(false);
    }
  };

  const updateField = (key: string, value: any) => {
    setEditingState(prev => ({
      ...prev,
      [key]: value,
      _state_hidden_keys: syncHiddenKeys(prev, (item: string) => normalizeStateKey(item) === normalizeStateKey(key))
    }));
  };

  const deleteField = (key: string) => {
    setEditingState(prev => {
      const normalizedKey = normalizeStateKey(key);
      const newState = { ...prev };
      delete newState[key];

      if (prev.status_changes && typeof prev.status_changes === 'object' && !Array.isArray(prev.status_changes)) {
        const nextStatusChanges = { ...prev.status_changes };
        Object.keys(nextStatusChanges).forEach((childKey) => {
          if (normalizeStateKey(childKey) === normalizedKey) {
            delete nextStatusChanges[childKey];
          }
        });
        newState.status_changes = nextStatusChanges;
      }

      const existing = Array.isArray(prev._state_hidden_keys) ? prev._state_hidden_keys : [];
      if (!existing.some((item: string) => normalizeStateKey(item) === normalizedKey)) {
        newState._state_hidden_keys = [...existing, normalizedKey];
      }
      return newState;
    });
  };

  const addArrayItem = (key: string, item: string) => {
    if (!item.trim()) return;
    setEditingState(prev => ({
      ...prev,
      [key]: [...(prev[key] || []), item.trim()]
    }));
  };

  const removeArrayItem = (key: string, index: number) => {
    setEditingState(prev => ({
      ...prev,
      [key]: (prev[key] || []).filter((_: any, i: number) => i !== index)
    }));
  };

  const updateObjectField = (parentKey: string, childKey: string, value: any) => {
    setEditingState(prev => ({
      ...prev,
      [parentKey]: {
        ...(prev[parentKey] || {}),
        [childKey]: value
      },
      ...(parentKey === 'status_changes'
        ? {
            _state_hidden_keys: syncHiddenKeys(prev, (item: string) => normalizeStateKey(item) === normalizeStateKey(childKey)),
          }
        : {})
    }));
  };

  const deleteObjectField = (parentKey: string, childKey: string) => {
    setEditingState(prev => {
      const normalizedKey = normalizeStateKey(childKey);
      const newParent = { ...(prev[parentKey] || {}) };
      delete newParent[childKey];
      const nextState = {
        ...prev,
        [parentKey]: newParent
      };

      if (parentKey === 'status_changes') {
        Object.keys(nextState).forEach((key) => {
          if (key === 'status_changes' || HIDDEN_WORLD_STATE_KEYS.has(key)) {
            return;
          }
          if (normalizeStateKey(key) === normalizedKey) {
            delete nextState[key];
          }
        });

        const existing = Array.isArray(prev._state_hidden_keys) ? prev._state_hidden_keys : [];
        if (!existing.some((item: string) => normalizeStateKey(item) === normalizedKey)) {
          nextState._state_hidden_keys = [...existing, normalizedKey];
        }
      }

      return nextState;
    });
  };

  const [newFieldKey, setNewFieldKey] = useState('');
  const [newFieldValue, setNewFieldValue] = useState('');

  const addNewField = () => {
    if (!newFieldKey.trim()) {
      message.warning('请输入字段名');
      return;
    }
    const normalizedKey = normalizeStateKey(newFieldKey.trim());
    setEditingState(prev => ({
      ...prev,
      [normalizedKey]: newFieldValue.trim(),
      _state_hidden_keys: syncHiddenKeys(prev, (item: string) => normalizeStateKey(item) === normalizedKey)
    }));
    setNewFieldKey('');
    setNewFieldValue('');
  };

  const renderFieldEditor = (key: string, value: any) => {
    // 数组类型
    if (Array.isArray(value)) {
      return (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontWeight: 500, marginBottom: 8 }}>{key}</div>
          <Space direction="vertical" style={{ width: '100%' }}>
            {value.map((item, index) => (
              <Space key={index} style={{ width: '100%' }}>
                <Input
                  value={item}
                  onChange={(e) => {
                    const newArray = [...value];
                    newArray[index] = e.target.value;
                    updateField(key, newArray);
                  }}
                  style={{ flex: 1 }}
                />
                <Button
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => removeArrayItem(key, index)}
                />
              </Space>
            ))}
            <Input
              placeholder="添加新项"
              onPressEnter={(e) => {
                addArrayItem(key, e.currentTarget.value);
                e.currentTarget.value = '';
              }}
              suffix={
                <Button
                  type="link"
                  size="small"
                  icon={<PlusOutlined />}
                  onClick={(e) => {
                    const input = (e.currentTarget.parentElement?.parentElement as HTMLInputElement);
                    if (input) {
                      addArrayItem(key, input.value);
                      input.value = '';
                    }
                  }}
                />
              }
            />
          </Space>
        </div>
      );
    }

    // 对象类型
    if (typeof value === 'object' && value !== null) {
      return (
        <div style={{ marginBottom: 16 }}>
          <div style={{ fontWeight: 500, marginBottom: 8 }}>{key}</div>
          <Space direction="vertical" style={{ width: '100%', paddingLeft: 16 }}>
            {Object.entries(value).map(([childKey, childValue]) => (
              <Space key={childKey} style={{ width: '100%' }}>
                <Tag color="geekblue">{childKey}</Tag>
                <Input
                  value={String(childValue)}
                  onChange={(e) => updateObjectField(key, childKey, e.target.value)}
                  style={{ flex: 1 }}
                />
                <Button
                  type="text"
                  danger
                  icon={<DeleteOutlined />}
                  onClick={() => deleteObjectField(key, childKey)}
                />
              </Space>
            ))}
            <Space style={{ width: '100%' }}>
              <Input
                placeholder="键"
                style={{ width: 120 }}
                onPressEnter={(e) => {
                  const keyInput = e.currentTarget;
                  const valueInput = keyInput.nextElementSibling as HTMLInputElement;
                  if (keyInput.value.trim() && valueInput) {
                    updateObjectField(key, keyInput.value.trim(), valueInput.value);
                    keyInput.value = '';
                    valueInput.value = '';
                  }
                }}
              />
              <Input
                placeholder="值"
                style={{ flex: 1 }}
                onPressEnter={(e) => {
                  const valueInput = e.currentTarget;
                  const keyInput = valueInput.previousElementSibling as HTMLInputElement;
                  if (keyInput && keyInput.value.trim()) {
                    updateObjectField(key, keyInput.value.trim(), valueInput.value);
                    keyInput.value = '';
                    valueInput.value = '';
                  }
                }}
              />
            </Space>
          </Space>
        </div>
      );
    }

    // 基本类型
    return (
      <div style={{ marginBottom: 16 }}>
        <Space style={{ width: '100%' }}>
          <div style={{ fontWeight: 500, minWidth: 120 }}>{key}</div>
          <Input
            value={String(value)}
            onChange={(e) => updateField(key, e.target.value)}
            style={{ flex: 1 }}
          />
          <Popconfirm
            title="确定删除此字段？"
            onConfirm={() => deleteField(key)}
            okText="删除"
            cancelText="取消"
          >
            <Button type="text" danger icon={<DeleteOutlined />} />
          </Popconfirm>
        </Space>
      </div>
    );
  };

  return (
    <Modal
      title="编辑世界状态"
      open={visible}
      onCancel={onCancel}
      width={720}
      footer={[
        <Button key="cancel" onClick={onCancel}>
          取消
        </Button>,
        <Button key="save" type="primary" loading={saving} onClick={handleSave}>
          保存
        </Button>,
      ]}
      styles={{ body: { maxHeight: '70vh', overflowY: 'auto' } }}
    >
      <div style={{ padding: '16px 0' }}>
        {(() => {
          const statusKeys = new Set(
            editingState.status_changes && typeof editingState.status_changes === 'object' && !Array.isArray(editingState.status_changes)
              ? Object.keys(editingState.status_changes).map((key) => normalizeStateKey(key))
              : []
          );

          return Object.entries(editingState)
            .filter(([key]) => {
              if (HIDDEN_WORLD_STATE_KEYS.has(key)) {
                return false;
              }

              if (key === 'status_changes') {
                return true;
              }

              return !statusKeys.has(normalizeStateKey(key));
            })
            .map(([key, value]) => (
              <div key={key}>{renderFieldEditor(key, value)}</div>
            ));
        })()}

        <div style={{ marginTop: 24, paddingTop: 16, borderTop: '1px solid #f0f0f0' }}>
          <div style={{ fontWeight: 500, marginBottom: 8 }}>添加新字段</div>
          <Space style={{ width: '100%' }}>
            <Input
              placeholder="字段名"
              value={newFieldKey}
              onChange={(e) => setNewFieldKey(e.target.value)}
              style={{ width: 150 }}
            />
            <Input
              placeholder="字段值"
              value={newFieldValue}
              onChange={(e) => setNewFieldValue(e.target.value)}
              style={{ flex: 1 }}
              onPressEnter={addNewField}
            />
            <Button type="primary" icon={<PlusOutlined />} onClick={addNewField}>
              添加
            </Button>
          </Space>
        </div>
      </div>
    </Modal>
  );
};

export default WorldStateEditor;

