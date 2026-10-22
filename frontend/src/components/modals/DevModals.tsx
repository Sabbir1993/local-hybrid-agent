import React from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { CustomizeModal } from './CustomizeModal';
import { MonitorDrawer } from './MonitorDrawer';
import { DocsModal } from './DocsModal';
import { PreviewModal } from './PreviewModal';

export const DevModals: React.FC = () => {
  const { activeModal } = useAppStore();

  return (
    <>
      {activeModal === 'customize' && <CustomizeModal />}
      {activeModal === 'monitor' && <MonitorDrawer />}
      {activeModal === 'docs' && <DocsModal />}
      <PreviewModal />
    </>
  );
};
