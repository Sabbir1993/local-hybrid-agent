import React from 'react';
import { useAppStore } from '../../stores/useAppStore';
import { CustomizeModal } from './CustomizeModal';
import { MonitorDrawer } from './MonitorDrawer';
import { DocsModal } from './DocsModal';
import { PreviewModal } from './PreviewModal';
import { DiagramModal } from './DiagramModal';

export const DevModals: React.FC = () => {
  const { activeModal, hasAnyPerm } = useAppStore();

  return (
    <>
      {activeModal === 'customize' && hasAnyPerm('skills.view', 'connectors.view', 'plugins.view', 'agents.view') && <CustomizeModal />}
      {activeModal === 'monitor' && <MonitorDrawer />}
      {activeModal === 'docs' && <DocsModal />}
      <PreviewModal />
      <DiagramModal />
    </>
  );
};
