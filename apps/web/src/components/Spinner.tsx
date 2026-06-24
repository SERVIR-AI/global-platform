import { cn } from '@/lib/utils';
import { FC } from 'react';

interface SpinnerProps {
  className?: string;
}

const Spinner: FC<SpinnerProps> = ({ className }) => {
  return <span className={cn('loading loading-spinner', className)} />;
};

export default Spinner;
