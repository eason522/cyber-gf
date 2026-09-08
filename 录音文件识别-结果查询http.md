用于查询录音文件识别标准版的转写结果，需传入任务 ID（task_id）获取对应任务的识别状态与结果

<div data-tips="true" data-tips-type="default" data-tips-is-title="true">说明</div>


<div data-tips="true" data-tips-type="default">本接口请求体为空json</div>


&nbsp;

<span data-label="purple">POST</span> https://openspeech.bytedance.com/api/v3/auc/bigmodel/query

&nbsp;


<span id="U2dCXzkM"></span>
### 请求头


**X\-Api\-Key ** `string` <span data-api-tag="require|g9zFYw">必选</span>

API Key 可从 [控制台>API Key管理](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default.) 获取

<div data-tips="true" data-tips-type="default" data-tips-is-title="true">说明</div>


<div data-tips="true" data-tips-type="default">同时支持<a href="https://console.volcengine.com/speech/service/10035">旧版控制台</a>的鉴权方式，详见<a href="https://www.volcengine.com/docs/6561/2534847?lang=zh">旧版控制台鉴权参考示例</a></div>




**X\-Api\-Resource\-Id ** `string` <span data-api-tag="require|g9zFYw">必选</span>

请求的模型版本，可选值：


* `volc.seedasr.auc`:豆包录音文件识别模型2.0

* `volc.bigasr.auc`：豆包录音文件识别模型1.0



**X\-Api\-Request\-Id ** `string` <span data-api-tag="require|g9zFYw">必选</span>

传入录音文件识别接口返回的`task_id`




<span id="WtD1SAXn"></span>
### 响应


**X\-Tt\-Logid ** `string`

服务端返回的 logid，方便定位问题



**X\-Api\-Status\-Code ** `string`

提交任务后服务端返回的状态码



**X\-Api\-Message ** `string`

提交任务后服务端返回的信息，OK 表示成功，其他值表示失败



**result** `list`

识别结果，识别成功后返回


**text ** `string`

音频识别结果文本，识别成功后返回



**utterances ** `string`

语音分句信息。满足以下条件时返回：


* 请求参数`show_utterances`设置为`true`

* 识别成功



**text ** `string`

语音文本内容。满足以下条件时返回：


* 请求参数`show_utterances`设置为`true`

* 识别成功



**start_time ** `int`

起始时间（毫秒）



**end_time ** `int`

结束时间（毫秒）





&nbsp;



