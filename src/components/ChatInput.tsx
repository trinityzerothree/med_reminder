import { useState, useRef, useCallback, KeyboardEvent, ChangeEvent } from 'react';
import { useT, MessageKeys } from '../i18n';
import styles from './ChatInput.module.css';

interface Props {
  onSend: (text: string, image?: { base64: string; mimeType: string }) => void;
  onStop: () => void;
  onClear: () => void;
  disabled: boolean;
}

export default function ChatInput({ onSend, onStop, onClear, disabled }: Props) {
  const [value, setValue] = useState('');
  const [pendingImage, setPendingImage] = useState<{ base64: string; mimeType: string; previewUrl: string } | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { t } = useT();

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      const result = reader.result as string;
      const base64 = result.split(',')[1];
      setPendingImage({ base64, mimeType: file.type, previewUrl: result });
    };
    reader.readAsDataURL(file);
    e.target.value = '';
  };

  const handleSend = useCallback(() => {
    const trimmed = value.trim();
    if ((!trimmed && !pendingImage) || disabled) return;
    onSend(trimmed, pendingImage ? { base64: pendingImage.base64, mimeType: pendingImage.mimeType } : undefined);
    setValue('');
    setPendingImage(null);
    if (textareaRef.current) textareaRef.current.style.height = 'auto';
  }, [value, pendingImage, disabled, onSend]);
