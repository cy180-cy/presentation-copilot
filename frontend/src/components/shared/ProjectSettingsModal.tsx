import React, { useState } from 'react';
import { X, FileText, Settings as SettingsIcon, Download, Sparkles, AlertTriangle, HelpCircle, Lightbulb } from 'lucide-react';
import { Button, Textarea, TextStyleSelector } from '@/components/shared';
import { useT } from '@/hooks/useT';
import { PRODUCT_MODE } from '@/config/product';

import { ASPECT_RATIO_OPTIONS } from '@/config/aspectRatio';

// ProjectSettings 组件自包含翻译
const projectSettingsI18n = {
  zh: {
    projectSettings: {
      title: "设置", projectConfig: "项目设置",  globalConfig: "全局设置",
      projectConfigTitle: "项目级配置", projectConfigDesc: "这些设置仅应用于当前项目，不影响其他项目",
      globalConfigTitle: "全局设置", globalConfigDesc: "这些设置应用于所有项目",
      aspectRatio: "画面比例", aspectRatioDesc: "设置生成幻灯片图片的画面比例",
      aspectRatioLocked: "已生成图片的项目无法调整画面比例",
      aspectRatioHelp: "部分模型仅支持特定的画面比例（如 16:9、4:3、1:1）。如果图片生成报错，可尝试切换画面比例后重试。",
      extraRequirements: "额外要求", extraRequirementsDesc: "在生成每个页面时，AI 会参考这些额外要求",
      extraRequirementsPlaceholder: "例如：使用紧凑的布局，顶部展示一级大纲标题，加入更丰富的PPT插图...",
      saveExtraRequirements: "保存额外要求",
      styleDescription: "风格描述", styleDescriptionDesc: "描述您期望的 PPT 整体风格，AI 将根据描述生成相应风格的页面",
      styleDescriptionPlaceholder: "例如：简约商务风格，使用深蓝色和白色配色，字体清晰大方，布局整洁...",
      saveStyleDescription: "保存风格描述",
      styleTip: "风格描述会在生成图片时自动添加到提示词中。如果同时上传了模板图片，风格描述会作为补充说明。",


      tip: "提示"
    },
    shared: { saving: "保存中..." }
  },
  en: {
    projectSettings: {
      title: "Settings", projectConfig: "Project Settings",  globalConfig: "Global Settings",
      projectConfigTitle: "Project-level Configuration", projectConfigDesc: "These settings only apply to the current project",
      globalConfigTitle: "Global Settings", globalConfigDesc: "These settings apply to all projects",
      aspectRatio: "Aspect Ratio", aspectRatioDesc: "Set the aspect ratio for generated slide images",
      aspectRatioLocked: "Cannot change aspect ratio after images have been generated",
      aspectRatioHelp: "Some models only support specific aspect ratios (e.g. 16:9, 4:3, 1:1). If image generation fails, try switching to a different aspect ratio.",
      extraRequirements: "Extra Requirements", extraRequirementsDesc: "AI will reference these extra requirements when generating each page",
      extraRequirementsPlaceholder: "e.g., Use compact layout, show first-level outline title at top, add richer PPT illustrations...",
      saveExtraRequirements: "Save Extra Requirements",
      styleDescription: "Style Description", styleDescriptionDesc: "Describe your expected PPT overall style, AI will generate pages in that style",
      styleDescriptionPlaceholder: "e.g., Simple business style, use navy blue and white colors, clear fonts, clean layout...",
      saveStyleDescription: "Save Style Description",
      styleTip: "Style description will be automatically added to the prompt when generating images. If a template image is also uploaded, the style description will serve as supplementary notes.",


      tip: "Tip"
    },
    shared: { saving: "Saving..." }
  }
};

interface ProjectSettingsModalProps {
  isOpen: boolean;
  onClose: () => void;
  extraRequirements: string;
  templateStyle: string;
  onExtraRequirementsChange: (value: string) => void;
  onTemplateStyleChange: (value: string) => void;
  onSaveExtraRequirements: () => void;
  onSaveTemplateStyle: () => void;
  isSavingRequirements: boolean;
  isSavingTemplateStyle: boolean;
  sourceContent?: string;


  aspectRatio?: string;
  onAspectRatioChange?: (value: string) => void;
  onSaveAspectRatio?: () => void;
  isSavingAspectRatio?: boolean;
  hasImages?: boolean;
}

type SettingsTab = 'project' | 'global';

export const ProjectSettingsModal: React.FC<ProjectSettingsModalProps> = ({
  isOpen,
  onClose,
  extraRequirements,
  templateStyle,
  onExtraRequirementsChange,
  onTemplateStyleChange,
  onSaveExtraRequirements,
  onSaveTemplateStyle,
  isSavingRequirements,
  isSavingTemplateStyle,
  sourceContent,


  aspectRatio = '16:9',
  onAspectRatioChange,
  onSaveAspectRatio,
  isSavingAspectRatio = false,
  hasImages = false,
}) => {
  const t = useT(projectSettingsI18n);
  const [activeTab, setActiveTab] = useState<SettingsTab>('project');


  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4">
      <div className="bg-white dark:bg-background-secondary rounded-xl shadow-2xl w-full max-w-5xl h-[90vh] flex flex-col overflow-hidden">
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200 dark:border-border-primary flex-shrink-0">
          <h2 className="text-xl font-bold text-gray-900 dark:text-foreground-primary">{t('projectSettings.title')}</h2>
          <button
            onClick={onClose}
            className="p-2 hover:bg-gray-100 dark:hover:bg-background-hover rounded-lg transition-colors"
            aria-label={t('common.close')}
          >
            <X size={20} />
          </button>
        </div>

        <div className="flex-1 flex overflow-hidden min-h-0">
          <aside className="w-64 bg-gray-50 dark:bg-background-primary border-r border-gray-200 dark:border-border-primary flex-shrink-0">
            <nav className="p-4 space-y-2">
              <button
                onClick={() => setActiveTab('project')}
                className={`w-full flex items-center gap-3 px-4 py-3 rounded-lg transition-all ${
                  activeTab === 'project'
                    ? 'bg-banana-500 text-white shadow-md'
                    : 'bg-white dark:bg-background-secondary text-gray-700 dark:text-foreground-secondary hover:bg-gray-100 dark:hover:bg-background-hover'
                }`}
              >
                <FileText size={20} />
                <span className="font-medium">{t('projectSettings.projectConfig')}</span>
              </button>

              {!PRODUCT_MODE && <button
                onClick={() => setActiveTab('global')}
                className={`w-full flex items-center gap-3 px-4 py-3 rounded-lg transition-all ${
                  activeTab === 'global'
                    ? 'bg-banana-500 text-white shadow-md'
                    : 'bg-white dark:bg-background-secondary text-gray-700 dark:text-foreground-secondary hover:bg-gray-100 dark:hover:bg-background-hover'
                }`}
              >
                <SettingsIcon size={20} />
                <span className="font-medium">{t('projectSettings.globalConfig')}</span>
              </button>}
            </nav>
          </aside>

          <div className="flex-1 overflow-y-auto p-6">
            {activeTab === 'project' ? (
              <div className="max-w-3xl space-y-6">
                <div>
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-foreground-primary mb-4">{t('projectSettings.projectConfigTitle')}</h3>
                  <p className="text-sm text-gray-600 dark:text-foreground-tertiary mb-6">
                    {t('projectSettings.projectConfigDesc')}
                  </p>
                </div>

                {/* 画面比例 */}
                <div className="pb-6 border-b border-gray-200 dark:border-border-primary space-y-4">
                  <div>
                    <div className="flex items-center gap-2 mb-2">
                      <h4 className="text-base font-semibold text-gray-900 dark:text-foreground-primary">{t('projectSettings.aspectRatio')}</h4>
                      <div className="relative group">
                        <button type="button" className="p-1 -m-1 rounded-full focus:outline-none focus:ring-2 focus:ring-banana-500">
                          <HelpCircle size={16} className="text-gray-400 dark:text-foreground-tertiary cursor-help" />
                        </button>
                        <div className="absolute left-1/2 -translate-x-1/2 bottom-full mb-2 w-64 p-2 bg-gray-900 dark:bg-gray-700 text-white text-xs rounded-lg opacity-0 invisible group-hover:opacity-100 group-hover:visible group-focus-within:opacity-100 group-focus-within:visible transition-all z-10 pointer-events-none">
                          {t('projectSettings.aspectRatioHelp')}
                        </div>
                      </div>
                    </div>
                    <p className="text-sm text-gray-600 dark:text-foreground-tertiary">
                      {hasImages ? t('projectSettings.aspectRatioLocked') : t('projectSettings.aspectRatioDesc')}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {ASPECT_RATIO_OPTIONS.map((opt) => (
                      <button
                        key={opt.value}
                        type="button"
                        disabled={hasImages}
                        onClick={() => onAspectRatioChange?.(opt.value)}
                        className={`px-4 py-2 text-sm font-medium rounded-lg border-2 transition-all disabled:opacity-50 disabled:cursor-not-allowed ${
                          aspectRatio === opt.value
                            ? 'border-banana-500 bg-banana-50 dark:bg-background-secondary text-banana-700 dark:text-banana'
                            : 'border-gray-200 dark:border-border-primary text-gray-700 dark:text-foreground-secondary hover:border-gray-300 dark:hover:border-gray-500 bg-white dark:bg-background-secondary'
                        }`}
                      >
                        {opt.label}
                      </button>
                    ))}
                  </div>
                  {onSaveAspectRatio && !hasImages && (
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={onSaveAspectRatio}
                      disabled={isSavingAspectRatio}
                      className="w-full sm:w-auto"
                    >
                      {isSavingAspectRatio ? t('shared.saving') : t('common.save')}
                    </Button>
                  )}
                </div>

                <div className="pb-6 border-b border-gray-200 dark:border-border-primary space-y-4">
                  <div>
                    <h4 className="text-base font-semibold text-gray-900 dark:text-foreground-primary mb-2">{t('projectSettings.extraRequirements')}</h4>
                    <p className="text-sm text-gray-600 dark:text-foreground-tertiary">
                      {t('projectSettings.extraRequirementsDesc')}
                    </p>
                  </div>
                  <Textarea
                    value={extraRequirements}
                    onChange={(e) => onExtraRequirementsChange(e.target.value)}
                    placeholder={t('projectSettings.extraRequirementsPlaceholder')}
                    rows={4}
                    className="text-sm"
                  />
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={onSaveExtraRequirements}
                    disabled={isSavingRequirements}
                    className="w-full sm:w-auto"
                  >
                    {isSavingRequirements ? t('shared.saving') : t('projectSettings.saveExtraRequirements')}
                  </Button>
                </div>

                <div className="space-y-4">
                  <div>
                    <h4 className="text-base font-semibold text-gray-900 dark:text-foreground-primary mb-2">{t('projectSettings.styleDescription')}</h4>
                    <p className="text-sm text-gray-600 dark:text-foreground-tertiary">
                      {t('projectSettings.styleDescriptionDesc')}
                    </p>
                  </div>
                  <TextStyleSelector
                    value={templateStyle}
                    onChange={onTemplateStyleChange}
                    sourceContent={sourceContent}
                  />
                  <div className="flex flex-col sm:flex-row gap-3">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={onSaveTemplateStyle}
                      disabled={isSavingTemplateStyle}
                      className="w-full sm:w-auto"
                    >
                      {isSavingTemplateStyle ? t('shared.saving') : t('projectSettings.saveStyleDescription')}
                    </Button>
                  </div>
                  <div className="pl-4 border-l-4 border-blue-300 dark:border-blue-600">
                    <p className="text-xs text-gray-700 dark:text-foreground-secondary flex items-start gap-1.5">
                      <Lightbulb size={14} className="flex-shrink-0 mt-0.5" />
                      <span><strong>{t('projectSettings.tip')}：</strong>{t('projectSettings.styleTip')}</span>
                    </p>
                  </div>
                </div>
              </div>
            ) : (
              <div className="max-w-4xl">
                <div className="mb-6">
                  <h3 className="text-lg font-semibold text-gray-900 dark:text-foreground-primary mb-2">{t('projectSettings.globalConfigTitle')}</h3>
                  <p className="text-sm text-gray-600 dark:text-foreground-tertiary">
                    {t('projectSettings.globalConfigDesc')}
                  </p>
                </div>
                <p>生成服务由开发者统一管理。</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
