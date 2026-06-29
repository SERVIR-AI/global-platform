import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';
import type { ChatItem } from '@/types/chat';

export const cn = (...inputs: ClassValue[]): string => twMerge(clsx(inputs));

/** Parse a `ChatItem`'s `created_at` into a `Date`, or `undefined` if absent/invalid. */
export const getChatItemDate = (item: ChatItem): Date | undefined => {
  if (!item.created_at) return undefined;
  const date = new Date(item.created_at);
  return Number.isNaN(date.getTime()) ? undefined : date;
};
